"""Recipe Builder window and its models, offscreen."""
import json
import os

import pytest

pytest.importorskip("qtpy")
from qtpy import QtCore  # noqa: E402


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
                             str(tmp_path_factory.mktemp("qsettings")))
    from corgidrp_gui.app import make_app
    return make_app([], restore=False)


@pytest.fixture
def builder(app, catalog):
    from corgidrp_gui.ui.builder_window import BuilderWindow
    w = BuilderWindow(catalog=catalog)
    w.show()
    yield w
    w._dirty = False
    w.close()


def _names(w):
    return w.recipe.step_names


def test_load_template_and_inspect(builder, catalog):
    w = builder
    w.load_template("l1_to_l2b.json")
    assert w.recipe.name == "l1_to_l2b" and not w.recipe.template
    assert w.level_chip.text() == "L1"
    assert w.steps_model.rowCount() == len(catalog.templates["l1_to_l2b.json"]["steps"])
    # select frame_select, see its args with defaults from the signature
    row = _names(w).index("frame_select")
    w.steps_view.setCurrentIndex(w.steps_model.index(row))
    am = w.args_model
    names = [am.rows[i].name for i in range(am.rowCount())]
    assert names[:3] == ["bpix_frac", "allowed_bpix", "overexp"]
    assert am.data(am.index(0, 2)) == "1.0"           # default column
    assert "frame_select" in w.step_title.text()
    assert "Selects the frames" in w.step_summary.text()
    # calibration rows for a step with slots
    row = _names(w).index("detect_cosmic_rays")
    w.steps_view.setCurrentIndex(w.steps_model.index(row))
    assert w.calib_grid.count() >= 6   # two slots x (label, type combo, spec combo)


def test_edit_argument_values(builder):
    w = builder
    w.load_template("l1_to_l2b.json")
    row = _names(w).index("frame_select")
    w.steps_view.setCurrentIndex(w.steps_model.index(row))
    am = w.args_model
    assert am.setData(am.index(0, 1), "0.05")
    step = w.recipe.steps[row]
    assert step.keywords["bpix_frac"] == 0.05 and am.is_changed(am.rows[0])
    assert am.setData(am.index(2, 1), "True")
    assert step.keywords["overexp"] is True
    assert "2 kwargs set" in w.steps_model.data(w.steps_model.index(row))
    am.setData(am.index(0, 1), "")                     # empty -> back to default
    assert "bpix_frac" not in step.keywords
    am.reset_all()
    assert step.keywords is None
    assert w._dirty and "modified" in w.status_chip.text()


def test_insert_move_remove_skip_undo(builder, catalog):
    w = builder
    w.load_template("l1_to_l2b.json")
    n = w.steps_model.rowCount()
    new_row = w.add_step("flat_division_pol")
    assert w.steps_model.rowCount() == n + 1
    assert _names(w)[new_row - 1] == "cti_correction"           # template-informed placement
    step = w.recipe.steps[new_row]
    assert step.calibs == {"FlatFieldPOL0": "AUTOMATIC", "FlatFieldPOL45": "AUTOMATIC"}
    w.steps_view._select_rows([new_row])
    w.move_selected(-1)
    assert _names(w)[new_row - 1] == "flat_division_pol"
    w.move_selected(+1)
    assert _names(w)[new_row] == "flat_division_pol"
    w.toggle_skip()
    assert w.recipe.steps[new_row].skip is True and "(skipped)" in w.steps_model.data(w.steps_model.index(new_row))
    w.toggle_skip()
    assert w.recipe.steps[new_row].skip is None
    w.duplicate_selected()
    assert _names(w)[new_row + 1] == "flat_division_pol"
    w.remove_selected()
    assert w.steps_model.rowCount() == n + 1
    w.undo()
    assert w.steps_model.rowCount() == n + 2
    # model-level drag/drop: move the block [1,2] to the end, and drop a catalog step at row 0
    m = w.steps_model
    first = _names(w)[:3]
    m.move_rows([1, 2], m.rowCount())
    assert _names(w)[-2:] == first[1:]
    from corgidrp_gui.ui.builder_models import STEP_MIME
    md = QtCore.QMimeData()
    md.setData(STEP_MIME, b"prescan_biassub")
    assert m.dropMimeData(md, QtCore.Qt.DropAction.CopyAction, 0, 0, QtCore.QModelIndex())
    assert _names(w)[0] == "prescan_biassub"


def test_issues_validation_and_json(builder):
    w = builder
    w.new_recipe()
    w.add_step("flat_division")
    txt = w.issues_label.text()
    assert "warning" in txt and "does not end with a save" in txt
    w.insert_save()
    assert "does not end with a save" not in w.issues_label.text()
    w.recipe.steps[0].keywords = {"nope": 1}
    w._refresh_issues()
    assert "1 error" in w.issues_label.text()
    d = json.loads(w.recipe.to_json())
    assert [s["name"] for s in d["steps"]] == ["flat_division", "save"]


def test_catalog_search_and_hint(builder):
    w = builder
    w.search.setText("polariz")
    f = w.catalog_filter
    shown = []
    for r in range(f.rowCount()):
        p = f.index(r, 0)
        shown += [f.index(c, 0, p).data() for c in range(f.rowCount(p))]
    assert "split_image_by_polarization_state" in shown and "prescan_biassub" not in shown, shown
    w.search.setText("")
    w.load_template("l1_to_l2b.json")
    src = w.catalog_model
    idx = None
    for r in range(src.rowCount()):
        p = src.index(r, 0)
        for c in range(src.rowCount(p)):
            if src.index(c, 0, p).data() == "flat_division_pol":
                idx = src.index(c, 0, p)
    w.catalog_view.selectionModel().select(w.catalog_filter.mapFromSource(idx), QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert "cti_correction" in w.catalog_hint.text()


def test_save_template_queue_and_parser_handoff(builder, mock_l1_files, tmp_path, monkeypatch):
    import corgidrp
    from corgidrp_gui.core.queue_items import QueueRequest
    from corgidrp_gui.core.recipes import Recipe, RecipeStep
    w = builder
    monkeypatch.setattr(corgidrp, "user_templates_dir", str(tmp_path / "user_templates"))
    r = Recipe(name="tiny_from_parser", inputs=list(mock_l1_files), outputdir=str(tmp_path / "out"),
               steps=[RecipeStep("discard_setup_frames", keywords={"keywords_to_check": ["ISACQ"]}), RecipeStep("save")])
    w.load_recipe(r)                                   # what the Parser's button does
    assert w.recipe.name == "tiny_from_parser" and w.level_chip.text() == "L1"
    assert "2 files" in w.inputs_label.fullText()
    path = w.save_as_template(os.path.join(corgidrp.user_templates_dir, "tiny_from_parser.json"))
    saved = json.load(open(path))
    assert saved["template"] is True and saved["inputs"] == [] and saved["outputdir"] == ""
    assert w.recipe.inputs == list(mock_l1_files)      # editor copy untouched
    got = []
    w.recipes_queued.connect(got.append)
    w.queue_recipe()
    assert len(got) == 1 and isinstance(got[0], QueueRequest) and got[0].source == "builder"
    assert got[0].chains[0][0].inputs == list(mock_l1_files)
    assert os.listdir(tmp_path / "out" / "recipes")
    from qtpy import QtWidgets
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2]) or None))
    w.recipe.steps[0].keywords = {"bad": 1}
    w._refresh_issues()
    w.queue_recipe()
    assert len(got) == 1 and warned and "no keyword argument 'bad'" in warned[0]


def test_value_popout_dialog(app, catalog):
    from corgidrp_gui.ui.dialogs import ValueEditorDialog
    from corgidrp_gui.ui.builder_models import needs_popout
    from corgidrp_gui.core.steps import MISSING
    spec = catalog.get("prescan_biassub").arg("detector_regions")
    assert spec is not None
    regions = {"SCI": {"image": {"rows": 1024, "cols": 1024, "r0c0": [13, 1088]}}}
    assert needs_popout(spec, regions) and not needs_popout(spec, None)
    assert needs_popout(catalog.get("discard_setup_frames").arg("keywords_to_check"), ["ISACQ"])
    d = ValueEditorDialog("detector_regions", regions, None, "dict", "detector geometry")
    assert '"rows": 1024' in d.text() and "dict with 1 key" in d.status.text()
    d.edit.setPlainText('{"SCI": {"image": {"rows": 2048}}, ')
    assert "not valid JSON" in d.status.text()
    d.edit.setPlainText("{'a': (1, 2)}")
    d.reformat()
    assert d.value() == {"a": [1, 2]} and d.text().startswith("{\n")
    d.edit.setPlainText("")
    assert d.value() is MISSING and "default" in d.status.text()
    d.close()
    d.deleteLater()
    app.processEvents()


def test_arg_description_popout(builder, app):
    from qtpy import QtWidgets
    w = builder
    w.load_template("l1_to_l2b.json")
    w.steps_view.setCurrentIndex(w.steps_model.index(_names(w).index("frame_select")))
    rows = [r for r in range(w.args_model.rowCount()) if w.args_model.rows[r].description]
    assert rows, "expected at least one documented argument"
    idx = w.args_model.index(rows[0], 4)
    spec = w.args_model.rows[rows[0]]
    text = w.arg_description_text(idx)
    assert text.startswith(spec.name) and spec.description in text and "Default:" in text
    d = w.show_arg_description(idx)
    assert d is not None and d.windowTitle() == f"Argument: {spec.name}"
    assert spec.description in d.findChild(QtWidgets.QPlainTextEdit).toPlainText()
    d.close()
    d.deleteLater()
    app.processEvents()


def test_start_from_template_dialog(builder, catalog, app, monkeypatch):
    from qtpy import QtWidgets
    from corgidrp_gui.ui.dialogs import TemplateChooserDialog
    w = builder
    assert "Empty recipe" in w.issues_label.text()
    d = TemplateChooserDialog(catalog, w)
    assert d.table.rowCount() == len(catalog.templates)
    d.filter.setText("l2b pol")
    shown = [d.table.item(r, 0).text() for r in range(d.table.rowCount())]
    assert shown and all("pol" in n and "l2b" in n for n in shown)
    d.filter.setText("frame_select")
    assert d.table.rowCount() and "frame_select" in d.preview.toPlainText()
    d.filter.setText("l1_to_l2b")
    names = [d.table.item(r, 0).text() for r in range(d.table.rowCount())]
    d.table.selectRow(names.index("l1_to_l2b"))
    assert d.selected() == "l1_to_l2b.json"
    assert d.table.item(names.index("l1_to_l2b"), 1).text() == "L1"
    d.close()
    d.deleteLater()
    app.processEvents()
    # the Builder's button runs the dialog and loads the chosen template
    monkeypatch.setattr(TemplateChooserDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(TemplateChooserDialog, "selected", lambda self: "l1_to_l2b.json")
    w.start_template_btn.click()
    assert w.recipe.name == "l1_to_l2b" and w.recipe.steps
    assert "l1_to_l2b.json" in w.origin_label.text()
    assert "Empty recipe" not in w.issues_label.text()


def test_builder_split_inputs(builder, mock_l1_files, tmp_path, monkeypatch):
    from qtpy import QtWidgets
    from corgidrp_gui.core.queue_items import QueueRequest
    w = builder
    w.load_template("l1_to_l2b.json")
    w.add_inputs(mock_l1_files)
    w.recipe.outputdir = str(tmp_path / "out")
    assert len(w.split_recipes()) == 1 and w.split_label.fullText() == ""
    w.set_split(max_files=1)
    rs = w.split_recipes()
    assert [r.name for r in rs] == ["l1_to_l2b_part1of2", "l1_to_l2b_part2of2"]
    assert [r.inputs for r in rs] == [[mock_l1_files[0]], [mock_l1_files[1]]]
    assert rs[0].steps and rs[0].to_dict()["steps"] == w.recipe.to_dict()["steps"]
    assert "2 recipes" in w.split_label.fullText()
    # keyword split reads headers through the backend (both mock frames share VISITID)
    w.set_split(max_files=0, keywords="visitid")
    assert w.split_keys_edit.text() == "VISITID" and len(w.split_recipes()) == 1
    assert "one recipe" in w.split_label.fullText()
    w.set_split(max_files=1, keywords="VISITID")
    rs = w.split_recipes()
    assert len(rs) == 2 and rs[0].name.startswith("l1_to_l2b_VISITID-") and rs[0].name.endswith("_part1of2")
    # queueing sends one chain per batch; saving writes one file per batch
    got = []
    w.queue_handler = lambda r: got.append(r) or True
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    w.queue_recipe()
    assert len(got) == 1 and isinstance(got[0], QueueRequest) and got[0].n_recipes == 2
    assert [c[0].inputs for c in got[0].chains] == [[mock_l1_files[0]], [mock_l1_files[1]]]
    written = w.save_recipe(str(tmp_path / "my_recipe.json"))
    assert len(written) == 2 and all(os.path.exists(p) for p in written)
    assert os.path.basename(written[0]).startswith("my_recipe_VISITID-") and written[1].endswith("_part2of2.json")
    w.set_split()
    assert len(w.split_recipes()) == 1


def test_builder_shows_unresolved_calibrations(builder, catalog):
    import corgidrp.walker as walker
    from corgidrp_gui.core.recipes import Recipe
    tpl, _path, _user = walker._load_recipe_template("l1_to_l2a_basic.json")
    r = Recipe.from_dict(tpl)
    r.template = False
    r.unresolved = [("correct_nonlinearity", "NonLinearityCalibration")]
    builder._dirty = False
    builder.load_recipe(r)
    assert builder.recipe.unresolved == r.unresolved
    msgs = [i.message for i in builder.current_issues()]
    assert any("no NonLinearityCalibration in the CalDB" in m for m in msgs)
    assert "NonLinearityCalibration" in builder.issues_label.text()


def test_builder_queue_resolves_automatic_calibrations(builder, mock_l1_files, nonlin_caldb, tmp_path, monkeypatch):
    from corgidrp_gui.core.recipes import Recipe, RecipeStep
    _db, nln = nonlin_caldb
    w = builder
    r = Recipe(name="calibs", inputs=list(mock_l1_files), outputdir=str(tmp_path / "out"), steps=[
        RecipeStep("correct_nonlinearity", calibs={"NonLinearityCalibration": "AUTOMATIC"}), RecipeStep("save")])
    w._dirty = False
    w.load_recipe(r)
    got = []
    w.queue_handler = lambda req: got.append(req) or True
    asked = []
    w._ask_incomplete = lambda inc, total: asked.append((inc, total)) or None
    w.queue_recipe()
    assert not asked and len(got) == 1
    assert got[0].chains[0][0].steps[0].calibs == {"NonLinearityCalibration": nln}
    assert w.recipe.steps[0].calibs == {"NonLinearityCalibration": "AUTOMATIC"}     # the editor copy keeps AUTOMATIC
    # split batches are each looked up
    w.set_split(max_files=1)
    w.queue_recipe()
    assert len(got) == 2 and [c[0].steps[0].calibs["NonLinearityCalibration"] for c in got[1].chains] == [nln, nln]
    w.set_split()


def test_builder_queue_asks_about_missing_calibrations(builder, mock_l1_files, nonlin_caldb, tmp_path):
    from corgidrp_gui.core.recipes import Recipe, RecipeStep
    w = builder
    r = Recipe(name="psfsub", inputs=list(mock_l1_files), outputdir=str(tmp_path / "out"), steps=[
        RecipeStep("do_psf_subtraction", calibs={"CoreThroughputCalibration": "AUTOMATIC"}), RecipeStep("save")])
    w._dirty = False
    w.load_recipe(r)
    got, asked = [], []
    w.queue_handler = lambda req: got.append(req) or True
    for answer, n_queued in ((None, 0), ("queue", 1)):
        w._ask_incomplete = lambda inc, total, a=answer: asked.append((len(inc), total)) or a
        w.queue_recipe()
        assert len(got) == n_queued
    assert asked == [(1, 1), (1, 1)]
    queued = got[0].chains[0][0]
    assert queued.steps[0].calibs == {"CoreThroughputCalibration": "AUTOMATIC"}
    assert queued.drpconfig.get("jit_calib_id") is True          # the walker retries the lookup at run time
    assert "jit_calib_id" not in w.recipe.drpconfig
    # a recipe that already asks for run-time lookup is queued as is, without asking
    asked.clear()
    w.recipe.drpconfig["jit_calib_id"] = True
    w.queue_recipe()
    assert not asked and len(got) == 2 and got[1].chains[0][0].steps[0].calibs == {"CoreThroughputCalibration": "AUTOMATIC"}
