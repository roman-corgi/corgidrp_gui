import json

from corgidrp_gui.core.recipes import Recipe, RecipeStep, validate_recipe


def test_templates_round_trip(catalog):
    for fname, path in catalog.template_paths.items():
        with open(path) as f:
            original = json.load(f)
        r = Recipe.load(path)
        assert r.to_dict() == original, fname
        assert json.loads(r.to_json()) == original, fname


def test_templates_validate_without_errors(catalog):
    for fname, path in catalog.template_paths.items():
        r = Recipe.load(path)
        start = catalog._template_start_level(fname, r.to_dict())
        errors = [i for i in validate_recipe(r, catalog, start) if i.severity == "error"]
        assert not errors, (fname, [str(e) for e in errors])


def test_validation_catches_bad_recipes(catalog):
    r = Recipe(name="bad", steps=[
        RecipeStep("no_such_step"),
        RecipeStep("frame_select", keywords={"bogus": 1}),
        RecipeStep("convert_to_electrons", calibs={"FlatField": "AUTOMATIC"}),
        RecipeStep("convert_to_electrons", keywords={"k_gain": "x"}),
    ])
    msgs = [str(i) for i in validate_recipe(r, catalog, "L2a")]
    assert any("unknown step" in m for m in msgs)
    assert any("no keyword argument 'bogus'" in m for m in msgs)
    assert any("usually KGain" in m for m in msgs)
    assert any("calibration argument" in m for m in msgs)
    assert any("does not end with a save" in m for m in msgs)


def test_new_step_from_catalog(catalog):
    s = RecipeStep.new(catalog.get("detect_cosmic_rays"))
    assert s.calibs == {"DetectorParams": "AUTOMATIC", "KGain": "AUTOMATIC, OPTIONAL"}
    assert s.keywords and "mode" in s.keywords
    s2 = RecipeStep.new(catalog.get("update_to_l2a"))
    assert s2.calibs is None and s2.keywords is None


def test_editing_and_templating():
    r = Recipe(name="x", inputs=["/a.fits"], outputdir="/out",
               steps=[RecipeStep("a"), RecipeStep("b"), RecipeStep("save")], extra={"RECIPE_SRC": "/p"})
    r.move_step(0, 1)
    assert r.step_names == ["b", "a", "save"]
    t = r.as_template("x_template")
    assert t.template and t.inputs == [] and t.outputdir == "" and "RECIPE_SRC" not in t.extra
    assert r.inputs == ["/a.fits"]   # original untouched
    s = r.steps[0]
    s.set_keyword("k", 1); s.unset_keyword("k")
    assert s.keywords is None


def _calib_recipe(inputs, nonlin="AUTOMATIC", ct="AUTOMATIC", optional="AUTOMATIC, OPTIONAL", drpconfig=None):
    return Recipe(name="calibs", inputs=list(inputs), outputdir="", drpconfig=dict(drpconfig or {}), steps=[
        RecipeStep("correct_nonlinearity", calibs={"NonLinearityCalibration": nonlin}),
        RecipeStep("prescan_biassub", calibs={"DetectorNoiseMaps": optional}),
        RecipeStep("do_psf_subtraction", calibs={"CoreThroughputCalibration": ct}),
        RecipeStep("save")])


def test_resolve_calibrations_fills_found_and_reports_missing(mock_l1_files, nonlin_caldb):
    from corgidrp_gui.core.recipes import resolve_calibrations
    _db, nln = nonlin_caldb
    r = _calib_recipe(mock_l1_files)
    missing = resolve_calibrations(r)
    assert missing == [("do_psf_subtraction", "CoreThroughputCalibration")] == r.unresolved
    assert r.steps[0].calibs["NonLinearityCalibration"] == nln
    assert r.steps[2].calibs["CoreThroughputCalibration"] == "AUTOMATIC"
    # a required gap keeps optional ones for the run-time lookup
    assert r.steps[1].calibs["DetectorNoiseMaps"] == "AUTOMATIC, OPTIONAL"
    # without a required gap, a missing optional calibration becomes None
    r = _calib_recipe(mock_l1_files, ct="/some/ct_cal.fits")
    assert resolve_calibrations(r) == [] and r.unresolved == []
    assert r.steps[1].calibs["DetectorNoiseMaps"] is None
    assert r.steps[2].calibs["CoreThroughputCalibration"] == "/some/ct_cal.fits"   # explicit files are left alone


def test_resolve_calibrations_respects_jit_and_skip_settings(mock_l1_files, nonlin_caldb, monkeypatch):
    import corgidrp
    from corgidrp_gui.core.recipes import jit_calib_id_on, resolve_calibrations
    _db, nln = nonlin_caldb
    r = _calib_recipe(mock_l1_files, drpconfig={"jit_calib_id": True})
    assert jit_calib_id_on(r) and resolve_calibrations(r) == []
    assert r.steps[0].calibs["NonLinearityCalibration"] == "AUTOMATIC"         # the walker looks it up at run time
    assert resolve_calibrations(r, force=True) and r.steps[0].calibs["NonLinearityCalibration"] == nln
    monkeypatch.setattr(corgidrp, "jit_calib_id", True)
    assert jit_calib_id_on(_calib_recipe([])) and not jit_calib_id_on(_calib_recipe([], drpconfig={"jit_calib_id": False}))
    monkeypatch.setattr(corgidrp, "jit_calib_id", False)
    assert resolve_calibrations(_calib_recipe([])) == []                      # no inputs, no reference frame
    monkeypatch.setattr(corgidrp, "skip_missing_cal_steps", True)
    r = _calib_recipe(mock_l1_files)
    assert resolve_calibrations(r) == []
    assert r.steps[2].skip is True and r.steps[2].calibs["CoreThroughputCalibration"] is None
