"""Fill AUTOMATIC calibrations from the CalDB before recipes are queued.

corgidrp's walker only looks AUTOMATIC calibrations up when it generates a recipe
itself, or when jit_calib_id is on; otherwise run_recipe tries to open a file named
"AUTOMATIC". The Builder and the Status Console both run recipes through
prepare_for_queue so the queued copies carry real calibration paths."""
from __future__ import annotations

import contextlib
import io
from typing import Callable

from qtpy import QtCore, QtWidgets

from ..core.recipes import Recipe, jit_calib_id_on, resolve_calibrations

Qt = QtCore.Qt
AskFn = Callable[[list[Recipe], int], "str | None"]


def has_automatic(recipe: Recipe) -> bool:
    return any("AUTOMATIC" in str(spec).upper() for s in recipe.steps for spec in (s.calibs or {}).values())


def resolve_with_progress(parent, recipes: list[Recipe]) -> bool:
    """Fill AUTOMATIC calibrations on each recipe (in place) from one CalDB, with a
    progress dialog when there are several. Returns False if the user cancelled.
    A failed lookup raises, with the recipe's name added to the message."""
    import corgidrp.caldb as caldb
    if not recipes:
        return True
    progress = None
    if len(recipes) > 1:
        progress = QtWidgets.QProgressDialog("Looking up calibrations in the CalDB…", "Cancel", 0, len(recipes), parent)
        progress.setWindowTitle("Queue recipes")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(500)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            db = caldb.CalDB()
            for i, r in enumerate(recipes):
                if progress is not None:
                    progress.setValue(i)
                    QtWidgets.QApplication.processEvents()
                    if progress.wasCanceled():
                        return False
                try:
                    resolve_calibrations(r, db)
                except Exception as exc:  # noqa: BLE001 - unreadable input, broken CalDB, ...
                    raise RuntimeError(f"{r.name}: {type(exc).__name__}: {exc}") from exc
    finally:
        if progress is not None:
            progress.close()
    return True


def ask_incomplete(parent, incomplete: list[Recipe], total: int) -> str | None:
    """Ask what to do with recipes whose required calibrations the CalDB lacks.
    Returns "queue", "skip" (queue only the complete ones) or None to cancel."""
    missing: dict[str, set[str]] = {}
    for r in incomplete:
        for step, label in r.unresolved:
            missing.setdefault(label, set()).add(step)
    names = "\n".join(f"• {label} (for {', '.join(sorted(steps))})" for label, steps in sorted(missing.items()))
    n = len(incomplete)
    who = "This recipe needs" if total == 1 else (f"All {total} recipes need" if n == total else f"{n} of {total} recipes need")
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    box.setWindowTitle("Missing calibrations")
    box.setText(f"{who} calibrations that the CalDB does not have for these files:")
    box.setInformativeText(names + "\n\nIf you queue them anyway, each step looks its calibration up again when "
                           "it runs, so it succeeds only if a job earlier in the queue makes one. "
                           "Otherwise add the calibration to the CalDB, or choose a file for the step in the Builder.")
    queue_btn = box.addButton("Queue anyway" if n == total else "Queue all", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    skip_btn = None
    if n < total:
        skip_btn = box.addButton(f"Queue the other {total - n}", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked is queue_btn:
        return "queue"
    if skip_btn is not None and clicked is skip_btn:
        return "skip"
    return None


def prepare_for_queue(parent, recipes: list[Recipe], ask: AskFn | None = None) -> list[Recipe] | None:
    """Resolve AUTOMATIC calibrations on the recipes about to be queued (in place) and
    ask about any the CalDB lacks. Recipes with jit_calib_id on are left alone, since
    the walker looks their calibrations up as each step runs.
    Returns the recipes to queue, or None when the user cancelled or the lookup failed."""
    if ask is None:
        ask = lambda inc, total: ask_incomplete(parent, inc, total)  # noqa: E731
    todo = [r for r in recipes if not jit_calib_id_on(r) and has_automatic(r)]
    try:
        if not resolve_with_progress(parent, todo):
            return None
    except Exception as exc:  # noqa: BLE001
        QtWidgets.QMessageBox.warning(parent, "Cannot queue", f"Looking up the AUTOMATIC calibrations failed:\n{exc}")
        return None
    incomplete = [r for r in todo if r.unresolved]
    if not incomplete:
        return recipes
    choice = ask(incomplete, len(recipes))
    if choice is None:
        return None
    if choice == "skip":
        skipped = {id(r) for r in incomplete}
        return [r for r in recipes if id(r) not in skipped]
    for r in incomplete:
        r.drpconfig["jit_calib_id"] = True   # the walker retries the lookup when the step runs
    return recipes
