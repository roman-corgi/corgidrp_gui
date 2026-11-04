"""Recipe Parser window, offscreen, on real mock L1 files."""
import json
import os

import pytest

pytest.importorskip("qtpy")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    from qtpy import QtCore
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
                             str(tmp_path_factory.mktemp("qsettings")))
    from corgidrp_gui.app import make_app
    return make_app([], restore=False)


@pytest.fixture
def tiny_template(tmp_path):
    p = tmp_path / "tiny_template.json"
    p.write_text(json.dumps({"name": "tiny", "template": True, "drpconfig": {}, "inputs": [], "outputdir": "",
                             "steps": [{"name": "discard_setup_frames", "keywords": {"keywords_to_check": ["ISACQ"]}},
                                       {"name": "save"}]}))
    return str(p)


def test_core_analyze_with_override(mock_l1_files, tiny_template, tmp_path):
    from corgidrp_gui.core.parser import analyze, scan_files
    recs = scan_files(mock_l1_files)
    res = analyze(recs, str(tmp_path / "out"), overrides={})
    assert len(res) == 1 and res[0].status in ("ok", "warn", "err") and res[0].message
    assert res[0].recommendation.chains == [["l1_to_l2a_basic.json"]]
    res = analyze(recs, str(tmp_path / "out"), overrides={res[0].group.key: tiny_template})
    a = res[0]
    assert a.status == "ok", a.message
    assert a.override == tiny_template and a.chains[0][0].inputs == mock_l1_files
    assert "chosen manually" in a.recommendation.reason


def test_parser_window_end_to_end(app, mock_l1_files, tiny_template, tmp_path):
    from corgidrp_gui.ui.parser_window import ParserWindow
    from corgidrp_gui.core.queue_items import QueueRequest
    out = tmp_path / "reduced"
    win = ParserWindow()
    win.show()
    win.outputdir_edit.setText(str(out))
    assert not win.queue_all_btn.isEnabled()

    win.add_files(mock_l1_files)
    assert win.jobs.wait_idle(120000)
    assert win.file_model.rowCount() == 2
    assert win.group_model.rowCount() == 1
    a = win.group_model.rows[0]
    assert a.group.label.startswith("L1") and a.recommendation.chains == [["l1_to_l2a_basic.json"]]
    assert "DATALVL = L1" in win.why.toPlainText()
    # files of the selected group are highlighted
    assert len(win.file_view.selectionModel().selectedRows()) == 2

    # manual override to a calibration-free template -> runnable
    win.group_view.selectRow(0)
    win.set_override(tiny_template)
    assert win.jobs.wait_idle(120000)
    a = win.group_model.rows[0]
    assert a.status == "ok" and a.override == tiny_template, a.message
    assert win.queue_all_btn.isEnabled() and win.preview_btn.isEnabled()
    assert "(manual)" in win.group_model.data(win.group_model.index(0, 3))

    # queue: signal carries a QueueRequest; default handler writes JSON files
    got = []
    win.recipes_queued.connect(got.append)
    win.queue_all()
    assert len(got) == 1 and isinstance(got[0], QueueRequest)
    req = got[0]
    assert req.n_recipes == 1 and req.outputdir == str(out) and req.chains[0][0].inputs == mock_l1_files
    files = os.listdir(out / "recipes")
    assert len(files) == 1 and files[0].endswith("_tiny.json")
    saved = json.load(open(out / "recipes" / files[0]))
    assert saved["steps"][0]["name"] == "discard_setup_frames" and saved["outputdir"] == str(out)

    # a custom queue handler replaces the file dump
    seen = []
    win.queue_handler = lambda r: seen.append(r) or True
    win.queue_selected()
    assert len(seen) == 1

    # adding the same files again is a no-op; removing clears groups
    win.add_files(mock_l1_files)
    assert win.file_model.rowCount() == 2
    win.clear_files()
    assert win.group_model.rowCount() == 0 and not win.queue_all_btn.isEnabled()
    win.close()


@pytest.mark.parametrize("key", ["roman", "console", "neon", "homepage", "midnight"])
def test_parser_window_under_every_theme(app, key):
    from corgidrp_gui import theming
    from corgidrp_gui.ui.parser_window import ParserWindow
    theming.theme_manager().set_theme(key, persist=False)
    win = ParserWindow()
    win.show()
    app.processEvents()
    assert win.isVisible()
    win.close()
    theming.theme_manager().set_theme("roman", persist=False)


def test_core_analyze_with_split(mock_l1_files, tiny_template, tmp_path):
    from corgidrp_gui.core.parser import analyze, scan_files, split_group, group_files
    from corgidrp_gui.core.splitting import SplitSpec
    recs = scan_files(mock_l1_files)
    groups = group_files(recs)
    assert len(groups) == 1 and len(split_group(groups[0], None)) == 1
    parts = split_group(groups[0], SplitSpec(max_files=1))
    assert [g.label for g in parts] == [groups[0].label + " · part 1/2", groups[0].label + " · part 2/2"]
    assert parts[0].override_key != parts[1].override_key and parts[0].override_key[:4] == groups[0].key
    # overrides are per batch and recipes get a distinguishing suffix
    res = analyze(recs, str(tmp_path / "out"), overrides={parts[1].override_key: tiny_template}, split=SplitSpec(max_files=1))
    assert [a.override for a in res] == [None, tiny_template]
    assert res[1].status == "ok" and res[1].chains[0][0].inputs == [mock_l1_files[1]]
    assert res[1].chains[0][0].name == "tiny_part2of2"
    # by keyword: both mock frames share a VISITID -> one batch, no suffix
    res = analyze(recs, str(tmp_path / "out"), overrides={}, split=SplitSpec(keywords=["VISITID"]))
    assert len(res) == 1 and res[0].group.batch is not None and "VISITID=" in res[0].group.label


def test_parser_window_split_controls(app, mock_l1_files, tiny_template, tmp_path):
    from corgidrp_gui.ui.parser_window import ParserWindow
    win = ParserWindow()
    win.show()
    win.outputdir_edit.setText(str(tmp_path / "reduced"))
    win.add_files(mock_l1_files)
    assert win.jobs.wait_idle(120000)
    assert win.group_model.rowCount() == 1
    win.set_split(max_files=1)
    assert win.jobs.wait_idle(120000)
    assert win.group_model.rowCount() == 2 and "part 1/2" in win.group_model.rows[0].group.label
    assert "1 files" in win.split_label.text()
    # an override applies to one batch only, and queueing yields one recipe per batch
    win.group_view.selectRow(1)
    win.set_override(tiny_template)
    assert win.jobs.wait_idle(120000)
    assert win.group_model.rows[1].status == "ok" and win.group_model.rows[0].override is None
    seen = []
    win.queue_handler = lambda r: seen.append(r) or True
    win.group_view.selectAll()
    win.set_override(tiny_template)
    assert win.jobs.wait_idle(120000)
    win.queue_all()
    assert len(seen) == 1 and seen[0].n_recipes == 2
    names = [c[0].name for c in seen[0].chains]
    assert names == ["tiny_part1of2", "tiny_part2of2"]
    assert [c[0].inputs for c in seen[0].chains] == [[mock_l1_files[0]], [mock_l1_files[1]]]
    # the file list highlights the selected batch only
    win.group_view.clearSelection()
    win.group_view.selectRow(0)
    assert len(win.file_view.selectionModel().selectedRows()) == 1
    win.set_split(max_files=0, keywords="visitid")
    assert win.jobs.wait_idle(120000)
    assert win.split_keys_edit.text() == "VISITID" and win.group_model.rowCount() == 1
    win.close()


def test_files_in_folders_and_add_folders(app, mock_l1_files, tmp_path):
    from corgidrp_gui.core.parser import files_in_folders
    from corgidrp_gui.ui.parser_window import ParserWindow
    a, b = tmp_path / "night_a", tmp_path / "night_b"
    (b / "sub").mkdir(parents=True)
    a.mkdir()
    import shutil
    pa = shutil.copy(mock_l1_files[0], a / "x_l1_.fits")
    pb = shutil.copy(mock_l1_files[1], b / "sub" / "y_l1_.fits")
    (a / "notes.txt").write_text("not fits")
    assert files_in_folders([str(a), str(b)]) == [str(pa)]
    assert files_in_folders([str(a), str(b)], recursive=True) == [str(pa), str(pb)]
    assert files_in_folders([str(a), str(a)], "*_l1_.fits") == [str(pa)]
    assert files_in_folders([str(b)], "x*", recursive=True) == []
    win = ParserWindow()
    win.show()
    win.add_folders([str(a), str(b)], recursive=True)
    assert win.jobs.wait_idle(120000)
    assert win.file_model.rowCount() == 2 and win._last_dir == str(a)
    win.clear_files()
    win.close()


def test_folder_picker_dialog_allows_multiple_selection(app, tmp_path):
    from qtpy import QtWidgets
    from corgidrp_gui.ui.dialogs import FolderPickerDialog
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    d = FolderPickerDialog(None, "pick", str(tmp_path))
    views = [v for v in d.findChildren(QtWidgets.QListView) + d.findChildren(QtWidgets.QTreeView)
             if v.objectName() in ("listView", "treeView")]
    assert views and all(v.selectionMode() == QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection for v in views)
    assert d.pattern == "*.fits" and not d.recursive
    d.pattern_edit.setText("  ")
    assert d.pattern == "*.fits"
    d.recursive_cb.setChecked(True)
    assert d.recursive
    d.close()
    d.deleteLater()
    app.processEvents()
