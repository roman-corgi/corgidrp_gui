"""Status Console: the recipe queue, one runner process per recipe, progress read
from the walker's console output, logs and tracebacks, abort and re-queue."""
from __future__ import annotations

import datetime as _dt
import os
import shlex
import time

from qtpy import QtCore, QtGui, QtWidgets

import corgidrp_gui

from .. import theming
from ..backend import default_backend
from ..core.queue import JobQueue, RecipeJob
from ..core.queue_items import QueueRequest
from .base import ThemedWindow
from .calib_check import ask_incomplete, prepare_for_queue
from .dialogs import RecipePreviewDialog, text_dialog
from .widgets.flow import FlowLayout
from .widgets.themed import Chip, ElidedLabel, Panel, SectionLabel, step_label

Qt = QtCore.Qt
_CHIP = {"waiting": "dim", "running": "run", "done": "ok", "failed": "err", "aborted": "warn", "skipped": "warn"}


def _fmt_elapsed(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class JobTableModel(QtCore.QAbstractTableModel):
    HEADERS = ["#", "Recipe", "Chain", "Files", "Status", "Progress", "Elapsed", "Message"]

    def __init__(self, queue: JobQueue, parent=None):
        super().__init__(parent)
        self.queue = queue

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()

    def refresh_row(self, job: RecipeJob):
        if job in self.queue.jobs:
            r = self.queue.jobs.index(job)
            self.dataChanged.emit(self.index(r, 0), self.index(r, self.columnCount() - 1))

    def tick(self):
        """Elapsed/progress columns for running jobs."""
        for j in self.queue.running:
            self.refresh_row(j)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.queue.jobs)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def job_at(self, row: int) -> RecipeJob | None:
        return self.queue.jobs[row] if 0 <= row < len(self.queue.jobs) else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        j = self.queue.jobs[index.row()]
        c = index.column()
        pal = theming.current_theme().palette
        if role == Qt.ItemDataRole.DisplayRole:
            return [index.row() + 1, j.name, j.chain_label, j.n_inputs, j.status, j.progress_text(),
                    _fmt_elapsed(j.elapsed), j.message][c]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{j.recipe_path}\noutput: {j.outputdir}\n{j.message}"
        if role == Qt.ItemDataRole.ForegroundRole and c == 4:
            return QtGui.QColor({"done": pal.ok, "failed": pal.err, "running": pal.secondary if pal.secondary != pal.bg else pal.primary,
                                 "aborted": pal.warn, "skipped": pal.warn, "waiting": pal.muted}[j.status])
        if role == Qt.ItemDataRole.FontRole and c == 4 and j.status in ("running", "failed"):
            f = QtGui.QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.TextAlignmentRole and c in (0, 3, 5, 6):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class ConsoleWindow(ThemedWindow):
    """GPI Status Console + Queue Viewer equivalent for corgidrp."""

    open_in_builder = QtCore.Signal(object)
    job_finished = QtCore.Signal(object)

    def __init__(self, parent=None, backend=None, queue: JobQueue | None = None, restore: bool = True):
        super().__init__(parent, title="Status Console")
        self.setObjectName("ConsoleWindow")
        self.backend = backend or default_backend()
        self.settings = theming.theme_manager().settings()
        self.queue = queue or JobQueue()
        self.builder_handler = None
        self._procs: dict[str, QtCore.QProcess] = {}
        self._bufs: dict[str, dict[str, str]] = {}
        self._aborting: set[str] = set()
        self._shown_job: RecipeJob | None = None
        self._all_log: list[str] = []          # everything ever shown in the combined log view
        self.model = JobTableModel(self.queue)
        self._build_menus()
        self._build_body()
        self.resize(1240, 800)
        geo = self.settings.value("console/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if restore:
            n = self.queue.load()
            if n:
                self.log_line(f"restored {n} queue entries from {self.queue.state_path()}")
        self.workers_spin.setValue(self.queue.max_workers)
        self.queue.listeners.append(self._on_queue_event)
        self.model.refresh()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._update_all()

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        q = self.add_menu("&Queue")
        self._act(q, "Add recipe file…", self.add_recipe_file_dialog, "Ctrl+O")
        q.addSeparator()
        self.pause_act = self._act(q, "Pause queue", self.toggle_pause, "Ctrl+P")
        self.pause_act.setCheckable(True)
        self._act(q, "Abort selected", self.abort_selected, "Ctrl+.")
        self._act(q, "Re-queue selected", self.requeue_selected, "Ctrl+R")
        self._act(q, "Remove selected", self.remove_selected, "Delete")
        self._act(q, "Clear finished", self.clear_finished)
        q.addSeparator()
        self._act(q, "Close", self.close, "Ctrl+W")
        v = self.add_menu("&View")
        self._act(v, "Recipe JSON", self.view_recipe)
        self._act(v, "Job log and traceback", self.show_details)
        self._act(v, "Open output folder", self.open_output_folder)
        self._act(v, "Copy runner command", self.copy_command)
        self.add_theme_menu()
        self.add_help_menu()

    @staticmethod
    def _act(menu, text, slot, shortcut=None):
        a = menu.addAction(text)
        a.triggered.connect(lambda _=False: slot())
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        return a

    def _build_body(self):
        theme = theming.current_theme()
        gap = theme.density.gap
        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(gap, gap, gap, gap)
        outer.setSpacing(gap)
        self.setCentralWidget(central)

        # queue panel
        qpanel = Panel(layout="v")
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setMinimumSectionSize(36)
        for c in (0, 2, 3, 4, 5, 6):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(1, 220)
        hh.setSectionResizeMode(7, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(lambda _i: self.show_details())
        qpanel.layout().addWidget(self.table, 1)
        brow = QtWidgets.QHBoxLayout()
        self.pause_btn = QtWidgets.QPushButton("Pause queue")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.clicked.connect(self.abort_selected)
        self.requeue_btn = QtWidgets.QPushButton("Re-queue")
        self.requeue_btn.clicked.connect(self.requeue_selected)
        self.remove_btn = QtWidgets.QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.up_btn = QtWidgets.QPushButton("▲")
        self.up_btn.clicked.connect(lambda: self.move_selected(-1))
        self.down_btn = QtWidgets.QPushButton("▼")
        self.down_btn.clicked.connect(lambda: self.move_selected(+1))
        self.builder_btn = QtWidgets.QPushButton("Open in Builder")
        self.builder_btn.clicked.connect(self.open_selected_in_builder)
        self.clear_btn = QtWidgets.QPushButton("Clear finished")
        self.clear_btn.clicked.connect(self.clear_finished)
        for b in (self.pause_btn, self.abort_btn, self.requeue_btn, self.remove_btn, self.up_btn, self.down_btn, self.builder_btn, self.clear_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        brow.addWidget(QtWidgets.QLabel("Workers"))
        self.workers_spin = QtWidgets.QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setToolTip("How many recipes may run at the same time")
        self.workers_spin.valueChanged.connect(self._on_workers)
        brow.addWidget(self.workers_spin)
        qpanel.layout().addLayout(brow)
        self.queue_label = SectionLabel("Queue")
        qsection = self._section(self.queue_label, qpanel)

        # current job panel
        cpanel = Panel(layout="v")
        self.cur_title = QtWidgets.QLabel("No recipe running")
        f = self.cur_title.font()
        f.setBold(True)
        self.cur_title.setFont(f)
        cpanel.layout().addWidget(self.cur_title)
        self.strip_host = QtWidgets.QWidget()
        self.strip = FlowLayout(self.strip_host, 0, 4, 4)
        cpanel.layout().addWidget(self.strip_host)
        self.cur_step_label = QtWidgets.QLabel("")
        cpanel.layout().addWidget(self.cur_step_label)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        cpanel.layout().addWidget(self.progress)
        kv = QtWidgets.QGridLayout()
        self.kv_labels: dict[str, ElidedLabel] = {}
        for r, key in enumerate(("inputs", "calibs", "output", "process")):
            k = QtWidgets.QLabel(key)
            k.setProperty("role", "hint")
            v = ElidedLabel("—")
            kv.addWidget(k, r, 0)
            kv.addWidget(v, r, 1)
            self.kv_labels[key] = v
        kv.setColumnStretch(1, 1)
        cpanel.layout().addLayout(kv)
        crow = QtWidgets.QHBoxLayout()
        for text, slot in (("Recipe JSON", self.view_recipe), ("Log && traceback", self.show_details),
                           ("Open output folder", self.open_output_folder), ("Copy command", self.copy_command)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: s())
            crow.addWidget(b)
        crow.addStretch(1)
        cpanel.layout().addLayout(crow)
        cpanel.layout().addStretch(1)
        self.cur_label = SectionLabel("Current recipe")
        csection = self._section(self.cur_label, cpanel)

        top = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(qsection)
        top.addWidget(csection)
        top.setSizes([720, 520])
        top.setChildrenCollapsible(False)

        # log panel
        lpanel = Panel(layout="v")
        lrow = QtWidgets.QHBoxLayout()
        self.follow_cb = QtWidgets.QCheckBox("Show only the selected job")
        self.follow_cb.toggled.connect(lambda _c: self._reload_log())
        lrow.addWidget(self.follow_cb)
        lrow.addStretch(1)
        clr = QtWidgets.QPushButton("Clear log view")
        clr.clicked.connect(self._clear_log_view)
        lrow.addWidget(clr)
        lpanel.layout().addLayout(lrow)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setProperty("role", "log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        lpanel.layout().addWidget(self.log, 1)
        lsection = self._section(SectionLabel("Log"), lpanel)

        main = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(lsection)
        main.setSizes([480, 280])
        main.setChildrenCollapsible(False)
        outer.addWidget(main, 1)

        self.status_chip = Chip("idle", "dim")
        self.statusBar().addPermanentWidget(self.status_chip)
        self.setAcceptDrops(True)

    @staticmethod
    def _section(label, body):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(body, 1)
        return w

    # --------------------------------------------------------------- enqueue
    def enqueue(self, req: QueueRequest) -> list[RecipeJob]:
        jobs = self.queue.add_request(req)
        self.log_line(f"queued {len(jobs)} recipe{'s' if len(jobs) != 1 else ''} from the {req.source}: "
                      + ", ".join(j.name + (f" [{j.chain_label}]" if j.is_chained else "") for j in jobs))
        self.queue.save()
        self.model.refresh()
        self.pump()
        self._update_all()
        return jobs

    def queue_recipe_files(self, paths: list[str]) -> list[RecipeJob]:
        """Queue recipe JSON files, each as its own job. AUTOMATIC calibrations are
        filled in from the CalDB first (the files on disk are not changed)."""
        from ..core.recipes import Recipe
        recipes = []
        for p in paths:
            try:
                r = Recipe.load(p)
            except (OSError, ValueError) as exc:
                self.log_line(f"skipped {p}: {exc}")
                continue
            if not r.inputs or not r.outputdir:
                self.log_line(f"skipped {os.path.basename(p)}: recipe needs inputs and an outputdir (edit it in the Builder)")
                continue
            recipes.append(r)
        if recipes:
            recipes = prepare_for_queue(self, recipes, ask=self._ask_incomplete)
        if not recipes:
            return []
        return self.enqueue(QueueRequest(chains=[[r] for r in recipes], outputdir=recipes[0].outputdir, source="file"))

    def _ask_incomplete(self, incomplete, total: int) -> str | None:
        return ask_incomplete(self, incomplete, total)

    def add_recipe_file_dialog(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Queue recipe files", self.backend.home(), "Recipe JSON (*.json)")
        if paths:
            self.queue_recipe_files(paths)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.toLocalFile().endswith(".json") for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().endswith(".json")]
        if paths:
            event.acceptProposedAction()
            self.queue_recipe_files(paths)

    # --------------------------------------------------------------- running
    def pump(self):
        """Start as many runnable jobs as the worker limit allows."""
        while True:
            job = self.queue.next_runnable()
            if job is None:
                break
            self.launch(job)

    def runner_command(self, job: RecipeJob) -> list[str]:
        return self.backend.runner_command(job.recipe_path, job.inputs_from, None)

    def launch(self, job: RecipeJob):
        self.queue.prepare_launch(job)
        cmd = self.runner_command(job)
        proc = QtCore.QProcess(self)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(corgidrp_gui.__file__)))
        pp = env.value("PYTHONPATH", "")
        if pkg_root not in pp.split(os.pathsep):
            env.insert("PYTHONPATH", pkg_root + (os.pathsep + pp if pp else ""))
        proc.setProcessEnvironment(env)
        # one pipe for stdout+stderr keeps tracebacks in order with the walker's prints
        proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        proc.setWorkingDirectory(job.outputdir if self.backend.isdir(job.outputdir) else self.backend.home())
        self._bufs[job.id] = {"stdout": "", "stderr": ""}
        proc.readyReadStandardOutput.connect(lambda j=job, p=proc: self._read(j, p, "stdout"))
        proc.readyReadStandardError.connect(lambda j=job, p=proc: self._read(j, p, "stderr"))
        proc.finished.connect(lambda code, status, j=job: self._on_finished(j, code, status))
        proc.errorOccurred.connect(lambda err, j=job, p=proc: self._on_proc_error(j, p, err))
        self._procs[job.id] = proc
        self.log_line(f"[{job.name}] $ {shlex.join(cmd)}", job)
        proc.start()
        job.pid = int(proc.processId()) if proc.processId() else None
        self.model.refresh_row(job)
        self._show_job(job)
        self._update_all()

    def _read(self, job: RecipeJob, proc: QtCore.QProcess, stream: str):
        data = proc.readAllStandardOutput() if stream == "stdout" else proc.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        buf = self._bufs.get(job.id, {"stdout": "", "stderr": ""})
        buf[stream] += text
        lines = buf[stream].split("\n")
        buf[stream] = lines.pop()          # partial last line stays in the buffer
        for line in lines:
            self._handle_line(job, line, stream)
        if job is self._shown_job:
            self._refresh_current()

    def _handle_line(self, job: RecipeJob, line: str, stream: str):
        events = job.parser.feed(line, stream)
        job.append_log(line)
        for ev in events:
            if ev.kind == "step":
                self.log_line(f"[{job.name}] {line}", job)
                self.model.refresh_row(job)
            elif ev.kind in ("error", "warning"):
                self.log_line(f"[{job.name}] {line}", job)
            elif ev.kind in ("recipe_start", "n_frames", "output", "finished", "runner"):
                self.log_line(f"[{job.name}] {line}", job)
            else:
                self._record(f"[{job.name}] {line}", job, raw=line)

    def _on_proc_error(self, job: RecipeJob, proc: QtCore.QProcess, err):
        if err == QtCore.QProcess.ProcessError.FailedToStart:
            job.append_log(f"failed to start: {proc.program()}")
            job.parser.errors.append(f"failed to start {proc.program()}")
            self.queue.mark_finished(job, -1)
            self._procs.pop(job.id, None)
            self._after_finish(job)

    def _on_finished(self, job: RecipeJob, code: int, status):
        proc = self._procs.pop(job.id, None)
        if proc is not None:
            for stream in ("stdout", "stderr"):   # flush partial lines
                self._read(job, proc, stream)
                rest = self._bufs.get(job.id, {}).get(stream, "")
                if rest.strip():
                    self._handle_line(job, rest, stream)
            proc.deleteLater()
        self._bufs.pop(job.id, None)
        crashed = status == QtCore.QProcess.ExitStatus.CrashExit
        self.queue.mark_finished(job, code if not crashed else -9, aborted=job.id in self._aborting)
        self._aborting.discard(job.id)
        self._after_finish(job)

    def _after_finish(self, job: RecipeJob):
        self.log_line(f"[{job.name}] {job.status}: {job.message} ({_fmt_elapsed(job.elapsed)})", job)
        self.queue.save()
        self.model.refresh()
        self.job_finished.emit(job)
        if job is self._shown_job:
            self._refresh_current()
        self.pump()
        self._update_all()

    def abort(self, job: RecipeJob):
        proc = self._procs.get(job.id)
        if proc is None or job.status != "running":
            return
        self._aborting.add(job.id)
        self.log_line(f"[{job.name}] aborting (SIGTERM)…", job)
        proc.terminate()
        QtCore.QTimer.singleShot(4000, lambda p=proc: p.kill() if p.state() != QtCore.QProcess.ProcessState.NotRunning else None)

    # --------------------------------------------------------------- actions
    def selected_jobs(self) -> list[RecipeJob]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.queue.jobs[r] for r in rows if r < len(self.queue.jobs)]

    def abort_selected(self):
        jobs = [j for j in self.selected_jobs() if j.status == "running"] or self.queue.running
        for j in jobs:
            self.abort(j)

    def requeue_selected(self):
        for j in self.selected_jobs():
            if j.status != "running":
                self.queue.requeue(j)
        self.queue.save()
        self.model.refresh()
        self.pump()
        self._update_all()

    def remove_selected(self):
        for j in self.selected_jobs():
            self.queue.remove(j)
        self.queue.save()
        self.model.refresh()
        self._update_all()

    def clear_finished(self):
        self.queue.clear_finished()
        self.queue.save()
        self.model.refresh()
        self._update_all()

    def move_selected(self, delta: int):
        jobs = self.selected_jobs()
        for j in (jobs if delta < 0 else reversed(jobs)):
            self.queue.move(j, delta)
        self.model.refresh()
        rows = [self.queue.jobs.index(j) for j in jobs]
        sm = self.table.selectionModel()
        sm.clearSelection()
        for r in rows:
            sm.select(self.model.index(r, 0), QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows)

    def toggle_pause(self):
        self.pause_btn.toggle()

    def _on_pause_toggled(self, checked: bool):
        self.queue.paused = checked
        self.pause_btn.setText("Resume queue" if checked else "Pause queue")
        self.pause_act.setChecked(checked)
        self.log_line("queue paused; running recipes continue" if checked else "queue resumed")
        if not checked:
            self.pump()
        self._update_all()

    def _on_workers(self, n: int):
        self.queue.max_workers = n
        self.pump()

    def open_selected_in_builder(self):
        jobs = self.selected_jobs() or ([self._shown_job] if self._shown_job else [])
        if not jobs:
            return
        if self.builder_handler is not None:
            self.builder_handler(jobs[0].recipe)
        self.open_in_builder.emit(jobs[0].recipe)

    def view_recipe(self):
        job = self._target_job()
        if job:
            RecipePreviewDialog([[job.recipe]], self, title=f"{job.name} — recipe").open()

    def show_details(self):
        job = self._target_job()
        if not job:
            return
        parts = [f"{job.name}  status: {job.status}  {job.message}", f"recipe: {job.recipe_path}",
                 f"command: {shlex.join(self.runner_command(job))}", ""]
        parts += job.log
        tb = job.parser.traceback_text()
        if tb and tb not in "\n".join(job.log):
            parts += ["", tb]
        text_dialog(self, f"{job.name} — log", "\n".join(parts))

    def open_output_folder(self):
        job = self._target_job()
        if job and self.backend.isdir(job.outputdir):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(job.outputdir))

    def copy_command(self):
        job = self._target_job()
        if job:
            QtWidgets.QApplication.clipboard().setText(shlex.join(self.runner_command(job)))
            self.statusBar().showMessage("Runner command copied; paste it in a terminal to reproduce this run", 6000)

    def _target_job(self) -> RecipeJob | None:
        sel = self.selected_jobs()
        return sel[0] if sel else self._shown_job

    # --------------------------------------------------------------- display
    def log_line(self, text: str, job: RecipeJob | None = None):
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self._record(f"{stamp}  {text}", job)

    def _record(self, text: str, job: RecipeJob | None, raw: str | None = None):
        """Keep ``text`` in the combined history and show it if the current filter allows."""
        self._all_log.append(text)
        if len(self._all_log) > 20000:
            del self._all_log[:5000]
        if self.follow_cb.isChecked():
            if job is not None and job is self._shown_job:
                self.log.appendPlainText(raw if raw is not None else text)
        else:
            self.log.appendPlainText(text)

    def _clear_log_view(self):
        self.log.clear()
        if not self.follow_cb.isChecked():
            self._all_log.clear()

    def _on_queue_event(self, event: str, job: RecipeJob | None):
        if event in ("added", "removed", "cleared", "reordered"):
            self.model.refresh()
        elif job is not None:
            self.model.refresh_row(job)

    def _on_selection(self, *_):
        sel = self.selected_jobs()
        if sel:
            self._show_job(sel[0])
        self._reload_log()
        self._update_buttons()

    def _show_job(self, job: RecipeJob | None):
        if job is self._shown_job:
            self._refresh_current()
            return
        self._shown_job = job
        self._refresh_current()
        if self.follow_cb.isChecked():
            self._reload_log()

    def _reload_log(self):
        """Rebuild the pane for the current filter: one job's own output, or everything."""
        if self.follow_cb.isChecked():
            if self._shown_job is None:
                return
            self.log.setPlainText("\n".join(self._shown_job.log))
        else:
            self.log.setPlainText("\n".join(self._all_log))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _refresh_current(self):
        job = self._shown_job
        self.strip.clear()
        if job is None:
            self.cur_title.setText("No recipe selected")
            self.cur_step_label.clear()
            self.progress.setValue(0)
            for v in self.kv_labels.values():
                v.setFullText("—")
            return
        title = f"{job.name}" + (f"  (chain {job.chain_label})" if job.is_chained else "") + f"  —  {job.status}"
        self.cur_title.setText(title)
        states = job.step_states()
        for name, state, el in states:
            text = name if el is None else f"{name} {el:.0f}s"
            lab = step_label(text, "done" if state == "done" else ("running" if state == "running" else "pending"))
            if state == "failed":
                lab.setProperty("chip", "err")
            elif state == "skipped":
                lab.setProperty("step", "pending")
                lab.setText(f"{name} (skip)")
            lab.setToolTip(f"{name}: {state}")
            self.strip.addWidget(lab)
        theming.decorate_tree(self.strip_host)
        p = job.parser
        total = p.total_steps or len(job.recipe.steps)
        done = len([s for s in p.steps if s.ended is not None])
        if job.status == "done":
            done = total
        self.progress.setValue(int(100 * done / total) if total else 0)
        if p.current_step and job.status == "running":
            self.cur_step_label.setText(f"Step {p.current_step.index} of {p.current_step.total}: {p.current_step.name}  ({_fmt_elapsed(p.current_step.elapsed)})")
        else:
            self.cur_step_label.setText(job.message or "")
        n = job.n_inputs
        first = os.path.basename(job.recipe.inputs[0]) if n else ""
        self.kv_labels["inputs"].setFullText(f"{n} file{'s' if n != 1 else ''}" + (f" · {first} …" if n else "") + (f" · {p.n_frames} frames read" if p.n_frames else ""))
        cal = job.resolved_calibs()
        self.kv_labels["calibs"].setFullText(", ".join(f"{label}: {os.path.basename(path)}" for _s, label, path in cal) if cal else "all AUTOMATIC / none")
        self.kv_labels["output"].setFullText(job.outputdir + (f" · {len(job.outputs)} files written" if job.outputs else ""))
        proc = f"pid {job.pid}" if job.pid else "not started"
        if job.returncode is not None:
            proc = f"exit {job.returncode}"
        self.kv_labels["process"].setFullText(f"{proc} · {_fmt_elapsed(job.elapsed)}")

    def _tick(self):
        if self.queue.running:
            self.model.tick()
            if self._shown_job in self.queue.running:
                self._refresh_current()

    def _update_buttons(self):
        sel = self.selected_jobs()
        self.abort_btn.setEnabled(bool(self.queue.running))
        self.requeue_btn.setEnabled(any(j.status != "running" for j in sel))
        self.remove_btn.setEnabled(any(j.status != "running" for j in sel))
        self.up_btn.setEnabled(any(j.status == "waiting" for j in sel))
        self.down_btn.setEnabled(any(j.status == "waiting" for j in sel))
        self.builder_btn.setEnabled(bool(sel) or self._shown_job is not None)
        self.clear_btn.setEnabled(any(not j.active for j in self.queue.jobs))

    def _update_all(self):
        c = self.queue.counts()
        running, waiting = c["running"], c["waiting"]
        self.setWindowTitle(f"Status Console — running {running} · queued {waiting} · done {c['done']}" + (f" · failed {c['failed']}" if c["failed"] else ""))
        self.queue_label.setRawText(f"Queue · {len(self.queue.jobs)}")
        if running:
            self.status_chip.setText(f"running {running}")
            self.status_chip.set_kind("run")
        elif self.queue.paused:
            self.status_chip.setText("paused")
            self.status_chip.set_kind("warn")
        else:
            self.status_chip.setText("idle")
            self.status_chip.set_kind("dim")
        self._update_buttons()
        if self._shown_job is None and self.queue.running:
            self._show_job(self.queue.running[0])

    def theme_changed(self, theme):
        self.model.layoutChanged.emit()
        self._refresh_current()

    # ---------------------------------------------------------------- closing
    def closeEvent(self, event):
        if self.queue.running:
            r = QtWidgets.QMessageBox.question(
                self, "Recipes are running", f"{len(self.queue.running)} recipe(s) are running. Abort them and close?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for j in list(self.queue.running):
                self.abort(j)
            for p in list(self._procs.values()):
                p.waitForFinished(5000)
        self._timer.stop()
        self.queue.save()
        self.settings.setValue("console/geometry", self.saveGeometry())
        super().closeEvent(event)

    # ---------------------------------------------------------------- testing
    def wait_idle(self, timeout_ms: int = 300000) -> bool:
        """Spin the event loop until nothing runs and nothing is runnable (tests)."""
        deadline = QtCore.QDeadlineTimer(timeout_ms)
        app = QtWidgets.QApplication.instance()
        while (self.queue.running or self.queue.next_runnable()) and not deadline.hasExpired():
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
            QtCore.QThread.msleep(20)
        app.processEvents()
        return not self.queue.running
