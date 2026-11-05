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


@pytest.fixture
def empty_caldb(tmp_path, monkeypatch):
    """Point corgidrp at a fresh, empty CalDB so every AUTOMATIC calibration is missing."""
    import corgidrp
    path = str(tmp_path / "empty_caldb.csv")
    monkeypatch.setattr(corgidrp, "caldb_filepath", path)
    return path


def test_core_incomplete_recipe_when_calibration_missing(mock_l1_files, tmp_path, empty_caldb, catalog):
    from corgidrp_gui.core.parser import analyze, scan_files
    from corgidrp_gui.core.recipes import validate_recipe
    res = analyze(scan_files(mock_l1_files), str(tmp_path / "out"))
    a = res[0]
    assert a.status == "incomplete" and a.incomplete and not a.complete, a.message
    assert a.message.startswith("incomplete: no ") and "in the CalDB for" in a.message
    r = a.chains[0][0]
    assert r.name.startswith("l1_to_l2a_basic") and r.inputs == mock_l1_files and r.outputdir == str(tmp_path / "out")
    assert r.unresolved and all(lab for _st, lab in r.unresolved)
    assert r.drpconfig.get("jit_calib_id") is True          # the walker retries the lookup at run time
    steps = {s.name: s for s in r.steps}
    for st, lab in r.unresolved:
        assert steps[st].calibs[lab].upper().startswith("AUTOMATIC")
    assert "missing calibration" in a.log
    # the Builder's validation names each gap; the JSON carries no GUI-only keys
    msgs = [i.message for i in validate_recipe(r, catalog)]
    assert sum("in the CalDB matched these files" in m for m in msgs) == len(r.unresolved)
    assert "unresolved" not in r.to_dict() and r.copy().unresolved == r.unresolved


def test_incomplete_builder_restores_settings_without_required_gaps(mock_l1_files, tmp_path, empty_caldb):
    from corgidrp_gui.core.parser import build_incomplete_recipes
    tpl = {"name": "opt_only", "template": True, "drpconfig": {"track_individual_errors": False}, "inputs": [],
           "outputdir": "", "steps": [{"name": "prescan_biassub", "calibs": {"DetectorNoiseMaps": "AUTOMATIC, OPTIONAL"}},
                                      {"name": "save"}]}
    chains, _log = build_incomplete_recipes(mock_l1_files, str(tmp_path), [[tpl]], False)
    r = chains[0][0]
    assert r.unresolved == [] and r.steps[0].calibs == {"DetectorNoiseMaps": None}
    assert r.drpconfig == {"track_individual_errors": False}
    assert tpl["steps"][0]["calibs"]["DetectorNoiseMaps"] == "AUTOMATIC, OPTIONAL"   # caller's dict untouched


def test_parser_window_incomplete_group(app, mock_l1_files, tiny_template, tmp_path, empty_caldb):
    from corgidrp_gui.ui.parser_window import ParserWindow
    from corgidrp_gui.core.parser import GroupAnalysis
    win = ParserWindow()
    win.show()
    win.outputdir_edit.setText(str(tmp_path / "reduced"))
    win.add_files(mock_l1_files)
    assert win.jobs.wait_idle(120000)
    a = win.group_model.rows[0]
    assert a.status == "incomplete"
    assert "incomplete" in win.statusBar().currentMessage()
    win.group_view.selectRow(0)
    assert "left as AUTOMATIC" in win.why.toPlainText()
    assert win.builder_btn.isEnabled() and win.preview_btn.isEnabled() and win.queue_all_btn.isEnabled()
    opened = []
    win.builder_handler = opened.append
    win.open_selected_in_builder()
    assert opened and opened[0].unresolved and "incomplete" in win.statusBar().currentMessage()

    # queueing asks: skip -> nothing to queue; queue -> in table order; cancel -> nothing
    queued = []
    win.queue_handler = lambda req: queued.append(req) or True
    for answer, expect in (([], 0), (None, 0), ("all", 1)):
        win._ask_incomplete = lambda inc, ans=answer: list(inc) if ans == "all" else ans
        from qtpy import QtWidgets
        orig = QtWidgets.QMessageBox.information
        QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            queued.clear()
            win.queue_all()
        finally:
            QtWidgets.QMessageBox.information = orig
        assert len(queued) == expect
    assert queued[0].chains[0][0].unresolved
    # a complete group alongside keeps the table order
    ok = GroupAnalysis(a.group, chains=[[a.chains[0][0].copy()]], status="ok")
    ok.chains[0][0].unresolved = []
    ok.chains[0][0].name = "complete_one"
    win.group_model.set_rows([a, ok])
    queued.clear()
    win.queue_all()
    assert [c[0].name for c in queued[0].chains] == [a.chains[0][0].name, "complete_one"]
    win.close()
