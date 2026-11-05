"""Recipe Parser window.

Load FITS files, see how corgidrp groups them and which recipe templates it
recommends for each group and why, preview the populated recipes, and queue
them. All corgidrp calls run in a worker thread; the window only touches the
filesystem through the backend.
"""
from __future__ import annotations

import datetime as _dt
import glob
import os
import re

from qtpy import QtCore, QtGui, QtWidgets

from .. import theming
from ..backend import default_backend
from ..core import catalog_cache
from ..core.parser import GroupAnalysis, FileRecord, analyze, analyze_group, files_in_folders, scan_files
from ..core.queue_items import QueueRequest
from ..core.splitting import SplitSpec
from .base import ThemedWindow
from .dialogs import RecipePreviewDialog, pick_folders, text_dialog
from .queue_bridge import dump_recipes
from .models import FileTableModel, GroupTableModel, ObjectRole
from .widgets.themed import Chip, ElidedLabel, Panel, SectionLabel, section
from .widgets.workers import JobRunner

Qt = QtCore.Qt
AUTO = "(automatic)"
BROWSE = "Choose a recipe file…"


class ParserWindow(ThemedWindow):
    """GPI Data Parser equivalent for corgidrp."""

    #: emitted with a QueueRequest when the user queues recipes
    recipes_queued = QtCore.Signal(object)
    #: emitted with a Recipe when the user asks to open one in the Builder
    open_in_builder = QtCore.Signal(object)

    def __init__(self, parent=None, backend=None):
        super().__init__(parent, title="Recipe Parser")
        self.setObjectName("ParserWindow")
        self.backend = backend or default_backend()
        self.jobs = JobRunner()
        self.settings = theming.theme_manager().settings()
        self._overrides: dict[tuple, str] = {}   # keyed by FileGroup.override_key
        self._split = SplitSpec()
        self.queue_handler = None      # callable(QueueRequest) -> bool; default saves JSON files
        self.builder_handler = None    # callable(Recipe)
        self.file_model = FileTableModel()
        self.group_model = GroupTableModel()
        self._build_menus()
        self._build_body()
        self.resize(1180, 760)
        self._restore_settings()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build_menus(self):
        m = self.add_menu("&File")
        self._act(m, "Add files…", self.add_files_dialog, "Ctrl+O")
        self._act(m, "Add folder…", self.add_folder_dialog, "Ctrl+Shift+O")
        self._act(m, "Add by wildcard…", self.add_wildcard_dialog, "Ctrl+W")
        m.addSeparator()
        self._act(m, "Save all recipes to folder…", self.save_all_recipes)
        m.addSeparator()
        self._act(m, "Close", self.close, "Ctrl+Q")
        e = self.add_menu("&Edit")
        self._act(e, "Remove selected files", self.remove_selected, "Delete")
        self._act(e, "Clear all files", self.clear_files)
        e.addSeparator()
        self._act(e, "Re-parse", self.reparse, "F5")
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

        # --- files panel ----------------------------------------------------
        files_panel = Panel(layout="v")
        row = QtWidgets.QHBoxLayout()
        for text, slot in (("Add files…", self.add_files_dialog), ("Add folder…", self.add_folder_dialog),
                           ("Wildcard…", self.add_wildcard_dialog)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(b)
        self.remove_btn = QtWidgets.QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_files)
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        files_panel.layout().addLayout(row)
        self.file_view = QtWidgets.QTableView()
        self.file_proxy = QtCore.QSortFilterProxyModel()
        self.file_proxy.setSourceModel(self.file_model)
        self.file_view.setModel(self.file_proxy)
        self.file_view.setSortingEnabled(True)
        self.file_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_view.verticalHeader().setVisible(False)
        self.file_view.horizontalHeader().setStretchLastSection(True)
        self.file_view.setAlternatingRowColors(False)
        self.file_view.doubleClicked.connect(self._open_file_externally)
        files_panel.layout().addWidget(self.file_view, 1)
        self.files_label = SectionLabel("Input files")
        files_section = self._section_with_label(self.files_label, files_panel)

        # --- groups panel ---------------------------------------------------
        groups_panel = Panel(layout="v")
        self.group_view = QtWidgets.QTableView()
        self.group_view.setModel(self.group_model)
        self.group_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.group_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.group_view.verticalHeader().setVisible(False)
        hh = self.group_view.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(36)
        for c in (0, 1, 2):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(4, 170)
        self.group_view.setWordWrap(False)
        self.group_view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.group_view.selectionModel().selectionChanged.connect(self._on_group_selected)
        self.group_view.doubleClicked.connect(lambda _i: self.preview_selected())
        groups_panel.layout().addWidget(self.group_view, 2)
        groups_panel.layout().addWidget(SectionLabel("Why this recipe"))
        self.why = QtWidgets.QPlainTextEdit()
        self.why.setReadOnly(True)
        self.why.setMaximumHeight(140)
        groups_panel.layout().addWidget(self.why, 1)
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(QtWidgets.QLabel("Override template"))
        self.override_combo = QtWidgets.QComboBox()
        self.override_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.override_combo.setMinimumContentsLength(28)
        self._fill_override_combo()
        self.override_combo.activated.connect(self._on_override_activated)
        orow.addWidget(self.override_combo, 1)
        groups_panel.layout().addLayout(orow)
        self.groups_label = SectionLabel("Recommended recipes")
        groups_section = self._section_with_label(self.groups_label, groups_panel)

        top = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(files_section)
        top.addWidget(groups_section)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)
        top.setSizes([620, 560])
        top.setChildrenCollapsible(False)
        self.top_splitter = top

        # --- controls + log -------------------------------------------------
        ctrl = Panel(layout="v")
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("Output dir"))
        self.outputdir_edit = QtWidgets.QLineEdit()
        self.outputdir_edit.setPlaceholderText("where reduced files and recipe copies go")
        self.outputdir_edit.editingFinished.connect(self._on_outputdir_changed)
        crow.addWidget(self.outputdir_edit, 3)
        b = QtWidgets.QPushButton("Browse…")
        b.clicked.connect(self.browse_outputdir)
        crow.addWidget(b)
        crow.addSpacing(gap)
        crow.addWidget(QtWidgets.QLabel("CPGS XML"))
        self.cpgs_edit = QtWidgets.QLineEdit()
        self.cpgs_edit.setPlaceholderText("optional; only find_spec_star reads it")
        crow.addWidget(self.cpgs_edit, 2)
        b2 = QtWidgets.QPushButton("Browse…")
        b2.clicked.connect(self.browse_cpgs)
        crow.addWidget(b2)
        ctrl.layout().addLayout(crow)

        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Max files per recipe"))
        self.max_files_spin = QtWidgets.QSpinBox()
        self.max_files_spin.setRange(0, 100000)
        self.max_files_spin.setSpecialValueText("no limit")
        self.max_files_spin.setToolTip("Groups with more files than this are cut into several recipes "
                                       "(part 1/n, 2/n, ...) to bound memory use. 0 = no limit.")
        self.max_files_spin.editingFinished.connect(self._on_split_changed)
        srow.addWidget(self.max_files_spin)
        srow.addSpacing(gap)
        srow.addWidget(QtWidgets.QLabel("Split by keywords"))
        self.split_keys_edit = QtWidgets.QLineEdit()
        self.split_keys_edit.setPlaceholderText("e.g. VISITID, DPAMNAME  (files with different values go into different recipes)")
        self.split_keys_edit.setClearButtonEnabled(True)
        self.split_keys_edit.setToolTip("Comma-separated header keywords (extension header first, then primary). "
                                        "Each distinct combination of values becomes its own recipe.")
        self.split_keys_edit.editingFinished.connect(self._on_split_changed)
        srow.addWidget(self.split_keys_edit, 2)
        self.split_label = QtWidgets.QLabel("")
        self.split_label.setProperty("role", "hint")
        srow.addWidget(self.split_label, 1)
        ctrl.layout().addLayout(srow)

        brow = QtWidgets.QHBoxLayout()
        self.reparse_btn = QtWidgets.QPushButton("Re-parse")
        self.reparse_btn.clicked.connect(self.reparse)
        self.preview_btn = QtWidgets.QPushButton("Preview recipe JSON")
        self.preview_btn.clicked.connect(self.preview_selected)
        self.builder_btn = QtWidgets.QPushButton("Open in Recipe Builder")
        self.builder_btn.clicked.connect(self.open_selected_in_builder)
        self.details_btn = QtWidgets.QPushButton("Details…")
        self.details_btn.setToolTip("corgidrp's own output and any traceback for the selected group")
        self.details_btn.clicked.connect(self.show_details)
        for w in (self.reparse_btn, self.preview_btn, self.builder_btn, self.details_btn):
            brow.addWidget(w)
        brow.addStretch(1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.hide()
        brow.addWidget(self.progress)
        self.queue_sel_btn = QtWidgets.QPushButton("Queue selected")
        self.queue_sel_btn.clicked.connect(self.queue_selected)
        self.queue_all_btn = QtWidgets.QPushButton("Queue all (calibrations first)")
        self.queue_all_btn.setProperty("role", "primary")
        self.queue_all_btn.clicked.connect(self.queue_all)
        brow.addWidget(self.queue_sel_btn)
        brow.addWidget(self.queue_all_btn)
        ctrl.layout().addLayout(brow)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setProperty("role", "log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        ctrl.layout().addWidget(self.log, 1)

        bottom = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        bottom.addWidget(top)
        bottom.addWidget(ctrl)
        bottom.setStretchFactor(0, 4)
        bottom.setStretchFactor(1, 1)
        bottom.setChildrenCollapsible(False)
        outer.addWidget(bottom, 1)
        self.main_splitter = bottom

        self.status_chip = Chip("idle", "dim")
        self.statusBar().addPermanentWidget(self.status_chip)
        self.statusBar().showMessage("Add files to begin.")

    @staticmethod
    def _section_with_label(label: SectionLabel, body: QtWidgets.QWidget) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(body, 1)
        return w

    def _fill_override_combo(self):
        self.override_combo.blockSignals(True)
        self.override_combo.clear()
        self.override_combo.addItem(AUTO)
        try:
            names = sorted(catalog_cache.load_catalog().templates)
        except Exception:  # noqa: BLE001 - no catalog yet
            names = []
        for n in names:
            self.override_combo.addItem(n)
        self.override_combo.insertSeparator(self.override_combo.count())
        self.override_combo.addItem(BROWSE)
        self.override_combo.blockSignals(False)

    # ------------------------------------------------------------- settings
    def _restore_settings(self):
        s = self.settings
        self.outputdir_edit.setText(s.value("parser/outputdir", "", type=str))
        self.cpgs_edit.setText(s.value("parser/cpgs_xml", "", type=str))
        self.max_files_spin.setValue(s.value("parser/split_max_files", 0, type=int))
        self.split_keys_edit.setText(s.value("parser/split_keywords", "", type=str))
        self._split = self.split_spec()
        self.split_label.setText(self._split.describe() if self._split.active else "")
        self._last_dir = s.value("parser/last_dir", self.backend.home(), type=str)
        geo = s.value("parser/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        s = self.settings
        s.setValue("parser/outputdir", self.outputdir_edit.text())
        s.setValue("parser/cpgs_xml", self.cpgs_edit.text())
        s.setValue("parser/split_max_files", self.max_files_spin.value())
        s.setValue("parser/split_keywords", self.split_keys_edit.text())
        s.setValue("parser/last_dir", self._last_dir)
        s.setValue("parser/geometry", self.saveGeometry())
        super().closeEvent(event)

    # --------------------------------------------------------------- helpers
    @property
    def outputdir(self) -> str:
        return self.outputdir_edit.text().strip()

    def split_spec(self) -> SplitSpec:
        """The splitting rules as currently entered in the controls."""
        return SplitSpec(max_files=self.max_files_spin.value(),
                         keywords=SplitSpec.parse_keywords(self.split_keys_edit.text()))

    def set_split(self, max_files: int = 0, keywords: str | list[str] = ""):
        """Set the splitting rules programmatically and re-parse."""
        self.max_files_spin.setValue(max_files)
        self.split_keys_edit.setText(keywords if isinstance(keywords, str) else ", ".join(keywords))
        self._on_split_changed()

    def _on_split_changed(self):
        spec = self.split_spec()
        self.split_keys_edit.setText(spec.keywords_text)   # normalised form
        if spec == self._split:
            return
        self._split = spec
        self.split_label.setText(spec.describe() if spec.active else "")
        if self.file_model.rowCount() and not self.jobs.busy:
            self.reparse()

    def log_line(self, text: str):
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"{stamp}  {text}")

    def _set_busy(self, busy: bool, text: str = ""):
        self.status_chip.setText(text or ("working" if busy else "idle"))
        self.status_chip.set_kind("run" if busy else "dim")
        self.progress.setVisible(busy)
        if not busy:
            self.progress.setValue(0)
        self._update_buttons()

    def _update_buttons(self):
        busy = self.jobs.busy
        have_files = self.file_model.rowCount() > 0
        sel = self.selected_analyses()
        runnable = [a for a in sel if a.chains]
        any_runnable = any(a.chains for a in self.group_model.rows)
        self.reparse_btn.setEnabled(have_files and not busy)
        self.preview_btn.setEnabled(bool(runnable) and not busy)
        self.builder_btn.setEnabled(bool(runnable) and not busy)
        self.details_btn.setEnabled(bool(sel))
        self.queue_sel_btn.setEnabled(bool(runnable) and not busy)
        self.queue_all_btn.setEnabled(any_runnable and not busy)
        self.remove_btn.setEnabled(have_files and not busy)
        self.clear_btn.setEnabled(have_files and not busy)
        self.override_combo.setEnabled(bool(sel) and not busy)
        self.files_label.setRawText(f"Input files · {self.file_model.rowCount()}")
        n = self.group_model.rowCount()
        self.groups_label.setRawText(f"Recommended recipes · {n} group{'s' if n != 1 else ''}")

    def selected_analyses(self) -> list[GroupAnalysis]:
        rows = sorted({i.row() for i in self.group_view.selectionModel().selectedRows()})
        return [self.group_model.rows[r] for r in rows]

    def _selected_file_rows(self) -> list[int]:
        return sorted({self.file_proxy.mapToSource(i).row() for i in self.file_view.selectionModel().selectedRows()})

    # ------------------------------------------------------------ add files
    def add_files_dialog(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add FITS files", self._last_dir,
                                                          "FITS files (*.fits *.fit *.fits.gz);;All files (*)")
        if paths:
            self._last_dir = os.path.dirname(paths[0])
            self.add_files(paths)

    def add_folder_dialog(self):
        folders, recursive, pattern = pick_folders(self, "Add every FITS file in one or more folders", self._last_dir)
        if folders:
            self.add_folders(folders, pattern, recursive)

    def add_folders(self, folders: list[str], pattern: str = "*.fits", recursive: bool = False):
        """Add every file matching ``pattern`` in the folders (and, optionally, their subfolders)."""
        self._last_dir = folders[0]
        paths = files_in_folders(folders, pattern, recursive, self.backend)
        where = folders[0] if len(folders) == 1 else f"{len(folders)} folders"
        if not paths:
            self.statusBar().showMessage(f"No files matching {pattern} in {where}", 5000)
        else:
            self.log_line(f"{len(paths)} files matching {pattern} in {where}" + (" (with subfolders)" if recursive else ""))
        self.add_files(paths)

    def add_wildcard_dialog(self):
        start = os.path.join(self._last_dir, "*_l1_.fits")
        pattern, ok = QtWidgets.QInputDialog.getText(self, "Add by wildcard", "Glob pattern (absolute path):", text=start)
        if ok and pattern.strip():
            paths = sorted(glob.glob(os.path.expanduser(pattern.strip())))
            if not paths:
                self.statusBar().showMessage("No files match that pattern", 5000)
            else:
                self._last_dir = os.path.dirname(paths[0])
            self.add_files(paths)

    def add_files(self, paths: list[str]):
        """Read headers of ``paths`` in the background, then (re)parse everything."""
        have = set(self.file_model.paths())
        new = [p for p in paths if p not in have]
        if not new:
            self.statusBar().showMessage("Those files are already loaded", 4000)
            return
        self.log_line(f"reading headers of {len(new)} file{'s' if len(new) != 1 else ''}…")
        self._set_busy(True, "reading headers")
        self.jobs.start(scan_files, new, self.backend, on_done=self._on_scanned, on_fail=self._on_job_failed,
                        on_progress=self._on_progress)

    def _on_scanned(self, records: list[FileRecord]):
        bad = [r for r in records if not r.ok]
        self.file_model.add_records(records)
        self.file_view.resizeColumnsToContents()
        if bad:
            self.log_line(f"{len(bad)} file{'s' if len(bad) != 1 else ''} could not be read: {bad[0].headers.error}")
        self.reparse()

    def remove_selected(self):
        rows = self._selected_file_rows()
        if rows:
            self.file_model.remove_rows(rows)
            self.reparse()

    def clear_files(self):
        self.file_model.clear()
        self.group_model.clear()
        self.why.clear()
        self._overrides.clear()
        self._set_busy(False)
        self.statusBar().showMessage("Add files to begin.")

    # --------------------------------------------------------------- parsing
    def reparse(self):
        """Group + recommend + build recipes for everything loaded (background)."""
        records = list(self.file_model.records)
        if not records:
            self.group_model.clear()
            self._update_buttons()
            return
        self._set_busy(True, "parsing")
        self.log_line(f"parsing {len(records)} files (headers only)…")
        self._split = self.split_spec()
        if self._split.active:
            self.log_line("splitting: " + self._split.describe())
        self.jobs.start(analyze, records, self.outputdir, dict(self._overrides), split=self._split,
                        on_done=self._on_analyzed, on_fail=self._on_job_failed, on_progress=self._on_progress)

    def _on_analyzed(self, analyses: list[GroupAnalysis]):
        self.group_model.set_rows(analyses)
        self.file_model.set_group_keys(analyses)
        for i, a in enumerate(analyses):
            self.log_line(f"group {i + 1} ({a.group.label}, {len(a.group.files)} files): {a.recipe_summary or '—'} · {a.message}")
        n_ok = sum(1 for a in analyses if a.complete)
        n_inc = sum(1 for a in analyses if a.incomplete)
        self._set_busy(False)
        n_batches = sum(1 for a in analyses if a.group.batch is not None)
        what = f"{len(analyses)} groups" if not n_batches else f"{len(analyses)} batches"
        inc = f" · {n_inc} incomplete (missing calibrations)" if n_inc else ""
        self.statusBar().showMessage(f"{self.file_model.rowCount()} files · {what} · {n_ok} with recipes ready{inc}")
        if analyses and not self.group_view.selectionModel().hasSelection():
            self.group_view.selectRow(0)
        self._update_buttons()

    def _on_progress(self, i, n, label):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.progress.setFormat(f"{label}  %v/%m" if label and label != "done" else "%v/%m")

    def _on_job_failed(self, tb: str):
        self._set_busy(False)
        self.log_line("error: " + tb.strip().splitlines()[-1])
        text_dialog(self, "Parser error", tb)

    def _on_outputdir_changed(self):
        if self.group_model.rowCount() and not self.jobs.busy:
            self.reparse()

    # ------------------------------------------------------------ selection
    def _on_group_selected(self, *_):
        sel = self.selected_analyses()
        if len(sel) == 1:
            a = sel[0]
            lines = []
            if a.recommendation:
                lines.append(a.recommendation.reason)
                lines.append("")
                lines.extend(a.recommendation.facts.lines())
            if a.message and a.status != "ok":
                lines += ["", a.message]
            if a.incomplete:
                lines.append("The recipe was still made. These calibrations are left as AUTOMATIC:")
                lines += [f"  • {lab} for {st} ({rn})" for rn, st, lab in a.missing_calibs]
                lines.append("Open it in the Recipe Builder to choose the files, or queue it after the "
                             "recipe that makes them: it looks them up again when the step runs.")
            self.why.setPlainText("\n".join(lines))
            self._sync_override_combo(a)
            # highlight the group's files
            smodel = self.file_view.selectionModel()
            smodel.clearSelection()
            flags = QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows
            for r in self.file_model.rows_for_group(a.group.override_key):
                smodel.select(self.file_proxy.mapFromSource(self.file_model.index(r, 0)), flags)
        elif not sel:
            self.why.clear()
        self._update_buttons()

    def _sync_override_combo(self, a: GroupAnalysis):
        self.override_combo.blockSignals(True)
        if a.override:
            i = self.override_combo.findText(a.override)
            if i < 0:
                self.override_combo.insertItem(1, a.override)
                i = 1
            self.override_combo.setCurrentIndex(i)
        else:
            self.override_combo.setCurrentIndex(0)
        self.override_combo.blockSignals(False)

    def _on_override_activated(self, index: int):
        text = self.override_combo.itemText(index)
        if text == BROWSE:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose a recipe or template", self._last_dir, "Recipe JSON (*.json)")
            if not path:
                self._on_group_selected()
                return
            text = path
            if self.override_combo.findText(path) < 0:
                self.override_combo.insertItem(1, path)
        self.set_override(None if text == AUTO else text)

    def set_override(self, template: str | None, rows: list[int] | None = None):
        """Force a template (name or path) for the selected groups and re-analyze them."""
        if rows is None:
            rows = sorted({i.row() for i in self.group_view.selectionModel().selectedRows()})
        if not rows:
            return
        for r in rows:
            a = self.group_model.rows[r]
            if template:
                self._overrides[a.group.override_key] = template
            else:
                self._overrides.pop(a.group.override_key, None)
            self.group_model.set_status(r, "working")
        self._set_busy(True, "resolving")
        self.jobs.start(self._reanalyze_rows, rows, template, on_done=self._on_rows_reanalyzed, on_fail=self._on_job_failed)

    def _reanalyze_rows(self, rows, template):
        out = []
        for r in rows:
            g = self.group_model.rows[r].group
            out.append((r, analyze_group(g, self.outputdir, template)))
        return out

    def _on_rows_reanalyzed(self, results):
        for r, a in results:
            self.group_model.update_row(r, a)
            self.log_line(f"group {r + 1}: {a.recipe_summary or '—'} · {a.message}")
        self._set_busy(False)
        self._on_group_selected()

    # --------------------------------------------------------------- actions
    def preview_selected(self):
        chains = [c for a in self.selected_analyses() if a.chains for c in a.chains]
        if chains:
            RecipePreviewDialog(chains, self).open()

    def show_details(self):
        sel = self.selected_analyses()
        if not sel:
            return
        parts = []
        for a in sel:
            parts.append(f"=== {a.group.label} · {len(a.group.files)} files · {a.status}: {a.message}")
            if a.recommendation and a.recommendation.log.strip():
                parts.append(a.recommendation.log.rstrip())
            if a.log.strip():
                parts.append(a.log.rstrip())
            parts.append("files:\n  " + "\n  ".join(a.group.paths))
        text_dialog(self, "Group details", "\n\n".join(parts))

    def open_selected_in_builder(self):
        sel = [a for a in self.selected_analyses() if a.chains]
        if not sel:
            return
        recipe = sel[0].chains[0][0]
        if recipe.unresolved:
            names = ", ".join(lab for _st, lab in recipe.unresolved)
            self.statusBar().showMessage(f"Opened an incomplete recipe: choose files for {names} "
                                         "in the Builder's Calibrations panel", 12000)
        if self.builder_handler is not None:
            self.builder_handler(recipe)
        self.open_in_builder.emit(recipe)
        if self.builder_handler is None and self.receivers(self.open_in_builder) <= 0:
            QtWidgets.QMessageBox.information(self, "Recipe Builder", "The Recipe Builder arrives in phase 3.")

    def queue_selected(self):
        self._queue([a for a in self.selected_analyses()])

    def queue_all(self):
        self._queue(list(self.group_model.rows))

    def _queue(self, analyses: list[GroupAnalysis]):
        ready = [a for a in analyses if a.complete]
        incomplete = [a for a in analyses if a.incomplete]
        not_ready = [a for a in analyses if not a.chains]
        if incomplete:
            extra = self._ask_incomplete(incomplete)
            if extra is None:
                return
            take = {id(a) for a in ready + extra}
            ready = [a for a in analyses if id(a) in take]   # keep the table's order (calibrations first)
        if not ready:
            QtWidgets.QMessageBox.information(self, "Nothing to queue", "None of the selected groups has a runnable recipe.")
            return
        if not_ready:
            names = "\n".join(f"• {a.group.label}: {a.message}" for a in not_ready)
            r = QtWidgets.QMessageBox.question(
                self, "Skip groups without recipes?",
                f"{len(not_ready)} group{'s' if len(not_ready) != 1 else ''} cannot be queued:\n{names}\n\nQueue the other {len(ready)}?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        if not self.outputdir:
            QtWidgets.QMessageBox.warning(self, "Output directory", "Set an output directory first.")
            return
        req = QueueRequest(chains=[c for a in ready for c in a.chains], outputdir=self.outputdir,
                           cpgs_xml=self.cpgs_edit.text().strip() or None, source="parser")
        handled = False
        if self.queue_handler is not None:
            handled = bool(self.queue_handler(req))
        self.recipes_queued.emit(req)
        if not handled and self.queue_handler is None:
            self._default_queue(req)

    def _ask_incomplete(self, incomplete: list[GroupAnalysis]) -> list[GroupAnalysis] | None:
        """Ask whether to queue recipes with missing calibrations. Returns the groups to add
        (all or none), or None to cancel."""
        names = "\n".join(f"• {a.group.label}: " + ", ".join(sorted({lab for _r, _s, lab in a.missing_calibs}))
                          for a in incomplete)
        n = len(incomplete)
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Incomplete recipes")
        box.setText(f"{n} group{'s' if n != 1 else ''} {'have' if n != 1 else 'has'} recipes with calibrations "
                    "the CalDB does not have yet:")
        box.setInformativeText(names + "\n\nThose recipes look the calibrations up again when each step runs, "
                               "so they succeed only if a job earlier in the queue makes them. "
                               "Otherwise open them in the Recipe Builder and choose the files.")
        queue_btn = box.addButton("Queue them too", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("Skip them", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(skip_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is queue_btn:
            return list(incomplete)
        if clicked is skip_btn:
            return []
        return None

    def _default_queue(self, req: QueueRequest, folder: str | None = None):
        """No console attached yet: write the recipes as JSON into <outputdir>/recipes/."""
        written = dump_recipes(req, folder, self.backend)
        where = os.path.dirname(written[0]) if written else folder
        self.log_line(f"no Status Console attached; wrote {len(written)} recipe file{'s' if len(written) != 1 else ''} to {where}")
        self.statusBar().showMessage(f"Wrote {len(written)} recipes to {where}", 8000)

    def save_all_recipes(self):
        rows = [a for a in self.group_model.rows if a.chains]
        if not rows:
            return
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder for recipe files", self.outputdir or self._last_dir)
        if d:
            self._default_queue(QueueRequest([c for a in rows for c in a.chains], d, source="parser"), folder=d)

    def browse_outputdir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Output directory", self.outputdir or self._last_dir)
        if d:
            self.outputdir_edit.setText(d)
            self._on_outputdir_changed()

    def browse_cpgs(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "CPGS XML", self._last_dir, "XML (*.xml);;All files (*)")
        if p:
            self.cpgs_edit.setText(p)

    def _open_file_externally(self, index):
        path = self.file_proxy.data(index, Qt.ItemDataRole.ToolTipRole)
        if path and self.backend.exists(path.splitlines()[0]):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path.splitlines()[0]))

    # --------------------------------------------------------------- theming
    def theme_changed(self, theme):
        self._update_buttons()
        self.group_model.layoutChanged.emit()
