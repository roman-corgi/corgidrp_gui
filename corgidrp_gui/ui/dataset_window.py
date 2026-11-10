"""Dataset Summary window: what is in a pile of FITS files.

Reads headers only (in the background), then groups the files along a chosen
hierarchy of header keywords, by default the ones corgidrp's walker and steps
branch on, and shows, for whatever group is selected, how every relevant keyword
is distributed and which files are in it.
"""
from __future__ import annotations

import os

from qtpy import QtCore, QtGui, QtWidgets

from .. import theming
from ..backend import default_backend
from ..core import dataset as ds
from ..core.parser import FileRecord, files_in_folders, scan_files
from .base import ThemedWindow
from .dialogs import pick_folders
from .models import FileTableModel, ObjectRole
from .widgets.themed import Chip, Panel, SectionLabel
from .widgets.workers import JobRunner

Qt = QtCore.Qt
NodeRole = Qt.ItemDataRole.UserRole + 20
CUSTOM = "Custom"
MODE_CATALOGUE = "Keywords corgidrp uses"
MODE_VARYING = "Every keyword that varies"
#: always shown in the file table after the grouping keywords
_FILE_EXTRA = ["DATETIME", "EXPTIME", "EMGAIN_C", "PA_APER"]


class SummaryFileModel(FileTableModel):
    """File table that also understands pseudo-keywords (PROCESSING) and formats values."""

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid() and index.column() > 0:
            rec = self.records[index.row()]
            if not rec.ok:
                return ""
            v = ds.value_of(rec, self.columns[index.column() - 1])
            return "" if v is None else ds.fmt_value(v)
        return super().data(index, role)

    def set_columns(self, columns: list[str]):
        self.beginResetModel()
        self.columns = list(columns)
        self.endResetModel()


class DatasetWindow(ThemedWindow):
    """Summary of a dataset, grouped by header keywords."""

    #: emitted with a list of paths when the user sends files to the Recipe Parser
    send_to_parser = QtCore.Signal(object)

    def __init__(self, parent=None, backend=None):
        super().__init__(parent, title="Dataset Summary")
        self.setObjectName("DatasetWindow")
        self.backend = backend or default_backend()
        self.jobs = JobRunner()
        self.settings = theming.theme_manager().settings()
        self.parser_handler = None          # callable(list[str]); set by the app
        self.records: list[FileRecord] = []
        self.root: ds.SummaryNode | None = None
        self.file_model = SummaryFileModel()
        self.setAcceptDrops(True)
        self._build_menus()
        self._build_body()
        self.resize(1280, 820)
        self._restore_settings()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build_menus(self):
        m = self.add_menu("&File")
        self._act(m, "Add files…", self.add_files_dialog, "Ctrl+O")
        self._act(m, "Add folder…", self.add_folder_dialog, "Ctrl+Shift+O")
        m.addSeparator()
        self._act(m, "Copy summary as text", self.copy_summary, "Ctrl+Shift+C")
        self._act(m, "Save file list of selected group…", self.save_file_list, "Ctrl+S")
        m.addSeparator()
        self._act(m, "Close", self.close, "Ctrl+Q")
        e = self.add_menu("&Edit")
        self._act(e, "Clear all files", self.clear_files)
        self._act(e, "Expand all groups", lambda: self.tree.expandAll())
        self._act(e, "Collapse all groups", lambda: self.tree.collapseAll())
        self.add_theme_menu()
        self.add_help_menu()

    @staticmethod
    def _act(menu, text, slot, shortcut=None):
        a = menu.addAction(text)
        a.triggered.connect(lambda _=False: slot())
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        return a

    @staticmethod
    def _labelled(label: SectionLabel, body: QtWidgets.QWidget) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(body, 1)
        return w

    def _build_body(self):
        theme = theming.current_theme()
        gap = theme.density.gap
        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(gap, gap, gap, gap)
        outer.setSpacing(gap)
        self.setCentralWidget(central)

        # --- controls ---------------------------------------------------------
        ctrl = Panel(layout="v")
        row = QtWidgets.QHBoxLayout()
        for text, slot in (("Add files…", self.add_files_dialog), ("Add folder…", self.add_folder_dialog)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(b)
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_files)
        row.addWidget(self.clear_btn)
        row.addSpacing(gap)
        self.totals_label = QtWidgets.QLabel("Add files or drop a folder here.")
        self.totals_label.setProperty("role", "hint")
        row.addWidget(self.totals_label, 1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        row.addWidget(self.progress)
        ctrl.layout().addLayout(row)

        grow = QtWidgets.QHBoxLayout()
        grow.addWidget(QtWidgets.QLabel("Group by"))
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItems(list(ds.PRESETS) + [CUSTOM])
        self.preset_combo.setToolTip("Ready-made groupings; editing the keywords makes it Custom")
        self.preset_combo.activated.connect(self._on_preset)
        grow.addWidget(self.preset_combo)
        self.hierarchy_edit = QtWidgets.QLineEdit()
        self.hierarchy_edit.setPlaceholderText("keywords, outermost first, e.g. VISITID › PSFREF › SATSPOTS")
        self.hierarchy_edit.setToolTip("Header keywords to group by, outermost first, separated by commas, spaces or ›. "
                                       "PROCESSING groups by the recipes the files went through. "
                                       "Double-click a keyword in the table on the right to add it.")
        self.hierarchy_edit.editingFinished.connect(self._on_hierarchy_edited)
        grow.addWidget(self.hierarchy_edit, 1)
        self.fold_cb = QtWidgets.QCheckBox("Fold levels where all files agree")
        self.fold_cb.setChecked(True)
        self.fold_cb.setToolTip("A keyword with one value in a group adds no level; it is listed under "
                                "'Same for all' instead, so the tree branches only where files differ.")
        self.fold_cb.toggled.connect(lambda _on: self.rebuild())
        grow.addWidget(self.fold_cb)
        ctrl.layout().addLayout(grow)
        outer.addWidget(ctrl)

        # --- groups tree ------------------------------------------------------
        tree_panel = Panel(layout="v")
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Group", "Files", "Same for all", "Varies within", "Time", "Exposure"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(0, 390)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        for c, w in ((2, 200), (3, 170), (4, 190), (5, 80)):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.Interactive)
            hh.resizeSection(c, w)
        self.tree.itemSelectionChanged.connect(self._on_node_selected)
        tree_panel.layout().addWidget(self.tree, 1)
        brow = QtWidgets.QHBoxLayout()
        self.parser_btn = QtWidgets.QPushButton("Send group to Recipe Parser")
        self.parser_btn.setProperty("role", "primary")
        self.parser_btn.setToolTip("Load the selected group's files into the Recipe Parser")
        self.parser_btn.clicked.connect(self.send_selected_to_parser)
        self.copy_btn = QtWidgets.QPushButton("Copy summary")
        self.copy_btn.setToolTip("Totals, the grouping tree and the keywords that vary, as plain text")
        self.copy_btn.clicked.connect(self.copy_summary)
        self.save_btn = QtWidgets.QPushButton("Save file list…")
        self.save_btn.clicked.connect(self.save_file_list)
        self.paths_btn = QtWidgets.QPushButton("Copy paths")
        self.paths_btn.clicked.connect(self.copy_paths)
        for b in (self.copy_btn, self.save_btn, self.paths_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        brow.addWidget(self.parser_btn)
        tree_panel.layout().addLayout(brow)
        self.tree_label = SectionLabel("Groups")
        tree_section = self._labelled(self.tree_label, tree_panel)

        # --- keyword table ----------------------------------------------------
        kw_panel = Panel(layout="v")
        krow = QtWidgets.QHBoxLayout()
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems([MODE_CATALOGUE, MODE_VARYING])
        self.mode_combo.setToolTip("Keywords corgidrp uses: what the walker and the steps branch and split on.\n"
                                   "Every keyword that varies: any header keyword with more than one value here.")
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._fill_keywords())
        krow.addWidget(self.mode_combo)
        self.hide_constant_cb = QtWidgets.QCheckBox("Only keywords that vary")
        self.hide_constant_cb.toggled.connect(lambda _on: self._fill_keywords())
        krow.addWidget(self.hide_constant_cb)
        krow.addStretch(1)
        kw_panel.layout().addLayout(krow)
        self.kw_table = QtWidgets.QTableWidget(0, 3)
        self.kw_table.setHorizontalHeaderLabels(["Keyword", "Kind", "Values (files)"])
        self.kw_table.verticalHeader().setVisible(False)
        self.kw_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.kw_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.kw_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.kw_table.setWordWrap(False)
        kh = self.kw_table.horizontalHeader()
        kh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        kh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        kh.setStretchLastSection(True)
        self.kw_table.cellDoubleClicked.connect(lambda r, _c: self.add_grouping_key(self._kw_at(r)))
        self.kw_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.kw_table.customContextMenuRequested.connect(self._kw_menu)
        kw_panel.layout().addWidget(self.kw_table, 1)
        self.kw_label = SectionLabel("Keywords")
        kw_section = self._labelled(self.kw_label, kw_panel)

        # --- files of the selected group -------------------------------------
        files_panel = Panel(layout="v")
        self.file_view = QtWidgets.QTableView()
        self.file_proxy = QtCore.QSortFilterProxyModel()
        self.file_proxy.setSourceModel(self.file_model)
        self.file_view.setModel(self.file_proxy)
        self.file_view.setSortingEnabled(True)
        self.file_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_view.verticalHeader().setVisible(False)
        self.file_view.horizontalHeader().setStretchLastSection(True)
        self.file_view.setWordWrap(False)
        self.file_view.horizontalHeader().setResizeContentsPrecision(60)   # size columns from a sample of rows
        self.file_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        files_panel.layout().addWidget(self.file_view, 1)
        self.files_label = SectionLabel("Files")
        files_section = self._labelled(self.files_label, files_panel)

        right = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        right.addWidget(kw_section)
        right.addWidget(files_section)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        right.setChildrenCollapsible(False)
        main = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(tree_section)
        main.addWidget(right)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)
        main.setSizes([760, 500])
        main.setChildrenCollapsible(False)
        self.main_splitter, self.right_splitter = main, right
        outer.addWidget(main, 1)

        self.status_chip = Chip("idle", "dim")
        self.statusBar().addPermanentWidget(self.status_chip)

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        preset = s.value("dataset/preset", ds.DEFAULT_PRESET, type=str)
        text = s.value("dataset/hierarchy", " › ".join(ds.PRESETS[ds.DEFAULT_PRESET]), type=str)
        self.hierarchy_edit.setText(text)
        self.preset_combo.setCurrentText(preset if preset in ds.PRESETS or preset == CUSTOM else ds.DEFAULT_PRESET)
        self._sync_preset()
        self.fold_cb.setChecked(s.value("dataset/fold", True, type=bool))
        self.mode_combo.setCurrentText(s.value("dataset/keyword_mode", MODE_CATALOGUE, type=str))
        self.hide_constant_cb.setChecked(s.value("dataset/only_varying", False, type=bool))
        self._last_dir = s.value("dataset/last_dir", self.backend.home(), type=str)
        geo = s.value("dataset/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        s = self.settings
        s.setValue("dataset/preset", self.preset_combo.currentText())
        s.setValue("dataset/hierarchy", self.hierarchy_edit.text())
        s.setValue("dataset/fold", self.fold_cb.isChecked())
        s.setValue("dataset/keyword_mode", self.mode_combo.currentText())
        s.setValue("dataset/only_varying", self.hide_constant_cb.isChecked())
        s.setValue("dataset/last_dir", self._last_dir)
        s.setValue("dataset/geometry", self.saveGeometry())
        super().closeEvent(event)

    # ------------------------------------------------------------ grouping
    def hierarchy(self) -> list[str]:
        return ds.parse_hierarchy(self.hierarchy_edit.text())

    def set_hierarchy(self, keys: list[str]):
        self.hierarchy_edit.setText(" › ".join(keys))
        self._sync_preset()
        self.rebuild()

    def _sync_preset(self):
        keys = self.hierarchy()
        match = next((name for name, ks in ds.PRESETS.items() if ks == keys), CUSTOM)
        self.preset_combo.setCurrentText(match)

    def _on_preset(self, _index=None):
        name = self.preset_combo.currentText()
        if name in ds.PRESETS:
            self.set_hierarchy(ds.PRESETS[name])

    def _on_hierarchy_edited(self):
        keys = self.hierarchy()
        self.hierarchy_edit.setText(" › ".join(keys))
        self._sync_preset()
        self.rebuild()

    def add_grouping_key(self, key: str | None, first: bool = False):
        if not key:
            return
        keys = [k for k in self.hierarchy() if k != key]
        keys = [key] + keys if first else keys + [key]
        self.set_hierarchy(keys)
        self.statusBar().showMessage(f"Grouping by {' › '.join(keys)}", 5000)

    def remove_grouping_key(self, key: str | None):
        if key:
            self.set_hierarchy([k for k in self.hierarchy() if k != key])

    # ------------------------------------------------------------ add files
    def add_files_dialog(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add FITS files", self._last_dir,
                                                          "FITS files (*.fits *.fit *.fits.gz);;All files (*)")
        if paths:
            self._last_dir = os.path.dirname(paths[0])
            self.add_files(paths)

    def add_folder_dialog(self):
        folders, recursive, pattern = pick_folders(self, "Summarise every FITS file in one or more folders", self._last_dir)
        if folders:
            self.add_folders(folders, pattern, recursive)

    def add_folders(self, folders: list[str], pattern: str = "*.fits", recursive: bool = False):
        self._last_dir = folders[0]
        paths = files_in_folders(folders, pattern, recursive, self.backend)
        if not paths:
            where = folders[0] if len(folders) == 1 else f"{len(folders)} folders"
            self.statusBar().showMessage(f"No files matching {pattern} in {where}", 5000)
            return
        self.add_files(paths)

    def add_files(self, paths: list[str]):
        """Read headers of new ``paths`` in the background, then regroup."""
        have = {r.path for r in self.records}
        new = [p for p in paths if p not in have]
        if not new:
            self.statusBar().showMessage("Those files are already loaded", 4000)
            return
        self._set_busy(True, f"reading {len(new)} headers")
        self.jobs.start(scan_files, new, self.backend, on_done=self._on_scanned, on_fail=self._on_failed,
                        on_progress=self._on_progress)

    def set_records(self, records: list[FileRecord]):
        """Show already-read files (from the Parser) without reading headers again."""
        self.records = list(records)
        self.rebuild()

    def _on_scanned(self, records: list[FileRecord]):
        self.records.extend(records)
        self._set_busy(False)
        bad = [r for r in records if not r.ok]
        if bad:
            self.statusBar().showMessage(f"{len(bad)} file{'s' if len(bad) != 1 else ''} could not be read: "
                                         f"{bad[0].headers.error}", 10000)
        self.rebuild()

    def _on_progress(self, i, n, _label=""):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.progress.setFormat("%v/%m headers")

    def _on_failed(self, tb: str):
        self._set_busy(False)
        QtWidgets.QMessageBox.warning(self, "Reading headers failed", tb.strip().splitlines()[-1])

    def clear_files(self):
        self.records = []
        self.rebuild()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        folders = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]
        if folders:
            files += files_in_folders(folders, "*.fits", False, self.backend)
            self._last_dir = folders[0]
        if files:
            self.add_files(files)
        event.acceptProposedAction()

    # ------------------------------------------------------------ building
    def rebuild(self):
        """Regroup everything loaded and refill the tree, keeping the selected group if it still exists."""
        keep = self._selected_path()
        self.tree.clear()
        if not self.records:
            self.root = None
            self.totals_label.setText("Add files or drop a folder here.")
            self.tree_label.setRawText("Groups")
            self._fill_keywords()
            self.file_model.set_records([])
            self.files_label.setRawText("Files")
            self._update_buttons()
            return
        self.root = ds.build_tree(self.records, self.hierarchy(), self.fold_cb.isChecked())
        shared = {sk for sk, _v in self.root.shared}
        self.file_model.set_columns(list(dict.fromkeys([k for k in self.hierarchy() if k not in shared] + _FILE_EXTRA)))
        self.tree.blockSignals(True)
        top = self._add_item(None, self.root)
        self.tree.expandToDepth(1)
        top.setExpanded(True)
        target = self._find_item(keep) or top
        self.tree.setCurrentItem(target)
        self.tree.blockSignals(False)
        n_groups = sum(1 for n in self.root.walk() if not n.children) if self.root.children else 1
        self.tree_label.setRawText(f"Groups · {n_groups} leaf group{'s' if n_groups != 1 else ''}")
        ok = [r for r in self.records if r.ok]
        span, exp = ds.time_span(ok), ds.total_exptime(ok)
        bits = [f"{len(self.records)} files"]
        if len(ok) != len(self.records):
            bits.append(f"{len(self.records) - len(ok)} unreadable")
        if span:
            bits.append(ds.fmt_span(span))
        if exp:
            bits.append(f"{ds.fmt_seconds(exp)} exposure")
        self.totals_label.setText(" · ".join(bits))
        self._on_node_selected()
        self._update_buttons()

    def _add_item(self, parent: QtWidgets.QTreeWidgetItem | None, node: ds.SummaryNode) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem()
        item.setData(0, NodeRole, node)
        item.setText(0, node.title)
        item.setText(1, str(node.n_files))
        item.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
        item.setText(2, node.shared_text)
        varies = self._varies_text(node)
        item.setText(3, varies)
        ok = [r for r in node.records if r.ok]
        item.setText(4, ds.fmt_span(ds.time_span(ok)))
        item.setText(5, ds.fmt_seconds(ds.total_exptime(ok)))
        info = ds.KEY_INFO.get(node.key or "")
        tip = [node.title, f"{node.n_files} files"]
        if info:
            tip.append(f"{node.key}: {info.why}")
        if node.shared:
            tip.append("Same for all: " + node.shared_text)
        if varies:
            tip.append("Varies within: " + varies)
        for c in range(6):
            item.setToolTip(c, "\n".join(tip))
        if node.key == "unreadable":
            item.setForeground(0, QtGui.QColor(theming.current_theme().palette.err))
        if parent is None:
            self.tree.addTopLevelItem(item)
            f = item.font(0)
            f.setBold(True)
            item.setFont(0, f)
        else:
            parent.addChild(item)
        for child in node.children:
            self._add_item(item, child)
        return item

    def _varies_text(self, node: ds.SummaryNode) -> str:
        """Catalogue keywords not on this node's path that still take several values in it."""
        fixed = {k for k, _ in node.path} | set(self.hierarchy())
        out = []
        for st in ds.keyword_stats(node.records):
            if st.key in fixed or st.constant:
                continue
            out.append(f"{st.key} ({st.n_unique})")
        return ", ".join(out)

    def _selected_path(self):
        node = self.selected_node()
        return node.path if node is not None else None

    def _find_item(self, path) -> QtWidgets.QTreeWidgetItem | None:
        if path is None:
            return None
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value().data(0, NodeRole)
            if node is not None and node.path == path:
                return it.value()
            it += 1
        return None

    # ------------------------------------------------------------ selection
    def selected_node(self) -> ds.SummaryNode | None:
        items = self.tree.selectedItems()
        return items[0].data(0, NodeRole) if items else None

    def _on_node_selected(self):
        node = self.selected_node()
        self._fill_keywords()
        records = node.records if node is not None else []
        self.file_model.set_records(records)
        self.file_view.resizeColumnsToContents()
        if node is not None:
            self.files_label.setRawText(f"Files · {node.n_files} in {node.title}")
        else:
            self.files_label.setRawText("Files")
        self._update_buttons()

    def _fill_keywords(self):
        node = self.selected_node()
        self.kw_table.setRowCount(0)
        if node is None:
            self.kw_label.setRawText("Keywords")
            return
        self.kw_label.setRawText(f"Keywords · {node.title}")
        self.hide_constant_cb.setEnabled(self.mode_combo.currentText() != MODE_VARYING)
        if self.mode_combo.currentText() == MODE_VARYING:
            stats = ds.varying_keywords(node.records)
        else:
            stats = ds.keyword_stats(node.records)
            if self.hide_constant_cb.isChecked():
                stats = [s for s in stats if not s.constant]
            else:   # varying first, then constant; absent keywords last
                stats = ([s for s in stats if not s.constant] + [s for s in stats if s.constant and not s.absent]
                         + [s for s in stats if s.absent])
        pal = theming.current_theme().palette
        grouped = set(self.hierarchy())
        self.kw_table.setRowCount(len(stats))
        for r, st in enumerate(stats):
            key_item = QtWidgets.QTableWidgetItem(st.key)
            kind = QtWidgets.QTableWidgetItem(st.category)
            vals = QtWidgets.QTableWidgetItem(st.values_text())
            tip = f"{st.key}" + (f": {st.why}" if st.why else "") + f"\n{st.n_unique} value{'s' if st.n_unique != 1 else ''}"
            tip += "\n" + "\n".join(f"  {ds.fmt_value(v)}: {n}" for v, n in st.counts[:40])
            tip += "\nDouble-click to group by it."
            for it in (key_item, kind, vals):
                it.setToolTip(tip)
                it.setData(ObjectRole, st.key)
            if not st.constant:
                f = key_item.font()
                f.setBold(True)
                key_item.setFont(f)
            else:
                for it in (key_item, kind, vals):
                    it.setForeground(QtGui.QColor(pal.muted))
            if st.key in grouped:
                key_item.setText(f"{st.key}  ▸")
            self.kw_table.setItem(r, 0, key_item)
            self.kw_table.setItem(r, 1, kind)
            self.kw_table.setItem(r, 2, vals)

    def _kw_at(self, row: int) -> str | None:
        it = self.kw_table.item(row, 0)
        return it.data(ObjectRole) if it is not None else None

    def _kw_menu(self, pos):
        key = self._kw_at(self.kw_table.rowAt(pos.y()))
        if not key:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(f"Group by {key} first", lambda: self.add_grouping_key(key, first=True))
        menu.addAction(f"Add {key} as the innermost level", lambda: self.add_grouping_key(key))
        if key in self.hierarchy():
            menu.addAction(f"Stop grouping by {key}", lambda: self.remove_grouping_key(key))
        menu.exec(self.kw_table.viewport().mapToGlobal(pos))

    # --------------------------------------------------------------- actions
    def selected_paths(self) -> list[str]:
        node = self.selected_node()
        return [r.path for r in node.records] if node is not None else []

    def send_selected_to_parser(self):
        paths = self.selected_paths()
        if not paths:
            return
        if self.parser_handler is not None:
            self.parser_handler(paths)
        self.send_to_parser.emit(paths)
        self.statusBar().showMessage(f"Sent {len(paths)} files to the Recipe Parser", 5000)

    def summary_text(self) -> str:
        return ds.summary_text(self.records, self.hierarchy(), self.fold_cb.isChecked()) if self.records else ""

    def copy_summary(self):
        text = self.summary_text()
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
            self.statusBar().showMessage("Summary copied to the clipboard", 4000)

    def copy_paths(self):
        paths = self.selected_paths()
        if paths:
            QtWidgets.QApplication.clipboard().setText("\n".join(paths))
            self.statusBar().showMessage(f"Copied {len(paths)} paths", 4000)

    def save_file_list(self, path: str | None = None):
        paths = self.selected_paths()
        if not paths:
            return None
        if path is None:
            node = self.selected_node()
            stem = "all_files" if node is None or node.key is None else f"{node.key}_{ds.fmt_value(node.value)}"
            suggestion = os.path.join(self._last_dir, f"{stem}.txt".replace(" ", "_"))
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save file list", suggestion, "Text (*.txt);;All files (*)")
            if not path:
                return None
        with open(path, "w") as f:
            f.write("\n".join(paths) + "\n")
        self.statusBar().showMessage(f"Wrote {len(paths)} paths to {path}", 6000)
        return path

    # --------------------------------------------------------------- state
    def _set_busy(self, busy: bool, text: str = ""):
        self.status_chip.setText(text or ("working" if busy else "idle"))
        self.status_chip.set_kind("run" if busy else "dim")
        self.progress.setVisible(busy)
        if not busy:
            self.progress.setValue(0)
        self._update_buttons()

    def _update_buttons(self):
        have = bool(self.records)
        sel = self.selected_node() is not None
        busy = self.jobs.busy
        self.clear_btn.setEnabled(have and not busy)
        self.parser_btn.setEnabled(sel and not busy)
        self.copy_btn.setEnabled(have)
        self.save_btn.setEnabled(sel)
        self.paths_btn.setEnabled(sel)

    def theme_changed(self, theme):
        self.rebuild()
