"""Status Console: real runner subprocesses on mock L1 files, offscreen."""
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


def _tiny(name, inputs, outputdir, suffix):
    from corgidrp_gui.core.recipes import Recipe, RecipeStep
    return Recipe(name=name, inputs=list(inputs), outputdir=outputdir,
                  steps=[RecipeStep("discard_setup_frames", keywords={"keywords_to_check": ["ISACQ"]}),
                         RecipeStep("save", keywords={"suffix": suffix})])


def test_console_runs_chain_and_reports_failure(app, mock_l1_files, tmp_path, monkeypatch):
    monkeypatch.setenv("CORGIDRP_GUI_HOME", str(tmp_path / "home"))
    from corgidrp_gui.core.queue import JobQueue
    from corgidrp_gui.core.queue_items import QueueRequest
    from corgidrp_gui.ui.console_window import ConsoleWindow
    out = str(tmp_path / "out")
    win = ConsoleWindow(queue=JobQueue(), restore=False)
    win.show()
    finished = []
    win.job_finished.connect(finished.append)

    chain = [_tiny("first", mock_l1_files, out, "s1"), _tiny("second", [], out, "s2")]
    jobs = win.enqueue(QueueRequest(chains=[chain], outputdir=out, source="parser"))
    assert [j.status for j in jobs] == ["running", "waiting"]
    assert "running 1" in win.windowTitle()
    assert win.wait_idle(300000), "chain did not finish in time"
    assert [j.status for j in jobs] == ["done", "done"], [j.message for j in jobs]
    assert len(jobs[0].outputs) == 2 and all(o.endswith("s1.fits") for o in jobs[0].outputs)
    assert jobs[1].recipe.inputs == jobs[0].outputs            # chained through --inputs-from
    assert all(o.endswith("s2.fits") and os.path.exists(o) for o in jobs[1].outputs)
    assert [s[1] for s in jobs[0].step_states()] == ["done", "done"]
    assert jobs[0].progress_text() == "2/2" and len(finished) == 2
    assert any("Walker step 1/2" in l for l in jobs[0].log)
    # table and log reflect it
    m = win.model
    assert m.data(m.index(0, 4)) == "done" and m.data(m.index(1, 2)) == "2/2"
    assert "Walker step" in win.log.toPlainText()
    # "show only the selected job" filters, and unticking restores the combined view
    full = win.log.toPlainText()
    assert "queued 2 recipes" in full
    win.table.selectRow(1)
    win.follow_cb.setChecked(True)
    only = win.log.toPlainText()
    assert "queued 2 recipes" not in only and "Walker step 1/2" in only and "[first]" not in only
    win.follow_cb.setChecked(False)
    restored = win.log.toPlainText()
    assert "queued 2 recipes" in restored and "[first]" in restored and "[second]" in restored
    # a saved queue state exists and names both jobs
    state = json.load(open(win.queue.state_path()))
    assert [j["status"] for j in state["jobs"]] == ["done", "done"]

    # failing recipe -> failed with step context; dependent skipped; re-queue runs it again
    bad = _tiny("bad", mock_l1_files, out, "x")
    bad.steps[0].name = "no_such_step"
    dep = _tiny("after_bad", [], out, "y")
    jobs2 = win.enqueue(QueueRequest(chains=[[bad, dep]], outputdir=out, source="builder"))
    assert win.wait_idle(300000)
    assert jobs2[0].status == "failed" and "KeyError" in jobs2[0].message, jobs2[0].message
    assert jobs2[1].status == "skipped"
    win.table.selectRow(win.queue.jobs.index(jobs2[0]))
    win.requeue_selected()
    assert win.wait_idle(300000)
    assert jobs2[0].status == "failed" and win.queue.counts()["skipped"] == 1
    # current panel shows the selected failed job
    win.table.selectRow(win.queue.jobs.index(jobs2[0]))
    assert "bad" in win.cur_title.text() and "failed" in win.cur_title.text()
    win.clear_finished()
    assert not win.queue.jobs
    win.close()


def test_console_pause_and_abort(app, mock_l1_files, tmp_path, monkeypatch):
    monkeypatch.setenv("CORGIDRP_GUI_HOME", str(tmp_path / "home"))
    from corgidrp_gui.core.queue import JobQueue
    from corgidrp_gui.core.queue_items import QueueRequest
    from corgidrp_gui.ui.console_window import ConsoleWindow
    out = str(tmp_path / "out")
    win = ConsoleWindow(queue=JobQueue(), restore=False)
    win.show()
    win.pause_btn.setChecked(True)
    jobs = win.enqueue(QueueRequest(chains=[[_tiny("p", mock_l1_files, out, "p")]], outputdir=out))
    assert jobs[0].status == "waiting" and "paused" in win.status_chip.text()
    win.pause_btn.setChecked(False)
    assert jobs[0].status == "running"
    win.abort(jobs[0])
    assert win.wait_idle(120000)
    assert jobs[0].status == "aborted" and jobs[0].message == "aborted by user"
    win.close()


def test_console_queue_files_resolves_calibrations(app, mock_l1_files, nonlin_caldb, tmp_path, monkeypatch):
    monkeypatch.setenv("CORGIDRP_GUI_HOME", str(tmp_path / "home"))
    from corgidrp_gui.core.queue import JobQueue
    from corgidrp_gui.core.recipes import Recipe, RecipeStep
    from corgidrp_gui.ui.console_window import ConsoleWindow
    _db, nln = nonlin_caldb
    out = str(tmp_path / "out")
    files = {}
    for name, label in (("nonlin", "NonLinearityCalibration"), ("psfsub", "CoreThroughputCalibration")):
        step = "correct_nonlinearity" if name == "nonlin" else "do_psf_subtraction"
        files[name] = str(tmp_path / f"{name}.json")
        Recipe(name=name, inputs=list(mock_l1_files), outputdir=out,
               steps=[RecipeStep(step, calibs={label: "AUTOMATIC"}), RecipeStep("save")]).save(files[name])
    files["empty"] = str(tmp_path / "empty.json")
    Recipe(name="empty", inputs=[], outputdir=out, steps=[RecipeStep("save")]).save(files["empty"])

    win = ConsoleWindow(queue=JobQueue(), restore=False)
    win.pause_btn.setChecked(True)
    asked = []
    paths = [files["nonlin"], files["psfsub"], files["empty"]]
    win._ask_incomplete = lambda inc, total: asked.append(([r.name for r in inc], total)) or None
    assert win.queue_recipe_files(paths) == [] and not win.queue.jobs
    assert asked == [(["psfsub"], 2)]

    win._ask_incomplete = lambda inc, total: "skip"
    jobs = win.queue_recipe_files(paths)
    assert [j.name for j in jobs] == ["nonlin"]
    assert Recipe.load(jobs[0].recipe_path).steps[0].calibs == {"NonLinearityCalibration": nln}
    assert Recipe.load(files["nonlin"]).steps[0].calibs == {"NonLinearityCalibration": "AUTOMATIC"}   # file untouched

    win._ask_incomplete = lambda inc, total: "queue"
    jobs = win.queue_recipe_files(paths)
    assert [j.name for j in jobs] == ["nonlin", "psfsub"]
    psf = Recipe.load(jobs[1].recipe_path)
    assert psf.steps[0].calibs == {"CoreThroughputCalibration": "AUTOMATIC"} and psf.drpconfig.get("jit_calib_id") is True
    win.close()
