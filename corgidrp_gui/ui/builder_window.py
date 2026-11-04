"""Recipe Builder window.

Catalog of step functions on the left, the recipe's steps in the middle (drag to
reorder, drop steps in from the catalog), and an inspector on the right with
every keyword argument, its default and docstring, the calibration slots,
inputs and pipeline settings. Recipes save as files or as user templates, and
queue through a QueueRequest.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

from qtpy import QtCore, QtGui, QtWidgets

from .. import theming
from ..backend import default_backend
from ..core import catalog_cache
from ..core.queue_items import QueueRequest
from ..core.recipes import Issue, Recipe, RecipeStep, guess_start_level, validate_recipe
from ..core.splitting import SplitSpec, batch_recipe_name, split_paths
from ..core.steps import MISSING, SAVE_STEP_NAME, StepCatalog
from ..core.values import format_value
from .base import ThemedWindow
from .builder_models import (ArgTableModel, CatalogFilter, RecipeStepsModel, RecipeStepsView,
                             StepCatalogModel, StepInfoRole, ValueDelegate, needs_popout)
from .dialogs import InputsDialog, RecipePreviewDialog, TemplateChooserDialog, text_dialog
from .queue_bridge import dump_recipes
from .widgets.themed import Chip, ElidedLabel, Panel, SectionLabel
from .widgets.workers import JobRunner

Qt = QtCore.Qt
_SPEC_CHOICES = ["AUTOMATIC", "AUTOMATIC, OPTIONAL", "None (skip this calibration)", "Choose file…"]


class BuilderWindow(ThemedWindow):
    """GPI Recipe Editor equivalent for corgidrp."""

    recipes_queued = QtCore.Signal(object)

    def __init__(self, parent=None, backend=None, catalog: StepCatalog | None = None):
        super().__init__(parent, title="Recipe Builder")
        self.setObjectName("BuilderWindow")
        self.backend = backend or default_backend()
        self.settings = theming.theme_manager().settings()
        self.jobs = JobRunner()
        self.catalog = catalog or catalog_cache.load_catalog()
        self.queue_handler = None
        self._template_origin: Recipe | None = None
        self._template_origin_name: str = ""
        self._undo: list[dict] = []
        self._dirty = False
        self._last_dir = self.settings.value("builder/last_dir", self.backend.home(), type=str)
        self.split = SplitSpec(max_files=self.settings.value("builder/split_max_files", 0, type=int),
                               keywords=SplitSpec.parse_keywords(self.settings.value("builder/split_keywords", "", type=str)))
        self._headers_cache: dict = {}
        self.steps_model = RecipeStepsModel(self.catalog, Recipe(name="new_recipe"))
        self.catalog_model = StepCatalogModel(self.catalog)
        self.args_model = ArgTableModel()
        self._build_menus()
        self._build_body()
        self.resize(1320, 820)
        geo = self.settings.value("builder/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.steps_model.changed.connect(self._on_recipe_changed)
        self.args_model.value_changed.connect(self._on_arg_changed)
        self._refresh_all()

    # ------------------------------------------------------------------ menus
    def _build_menus(self):
        m = self.add_menu("&File")
        self._act(m, "New recipe", self.new_recipe, "Ctrl+N")
        self._act(m, "Start from template…", self.start_from_template_dialog, "Ctrl+T")
        self._act(m, "Open recipe…", self.open_recipe_dialog, "Ctrl+O")
        self._act(m, "Add input files…", self.add_input_files_dialog, "Ctrl+I")
        self.template_menu = m.addMenu("New from template")
        self._fill_template_menu()
        m.addSeparator()
        self._act(m, "Save recipe…", self.save_recipe_dialog, "Ctrl+S")
        self._act(m, "Save as user template…", self.save_as_template_dialog, "Ctrl+Shift+S")
        m.addSeparator()
        self._act(m, "Close", self.close, "Ctrl+W")
        e = self.add_menu("&Edit")
        self._act(e, "Undo", self.undo, "Ctrl+Z")
        e.addSeparator()
        self._act(e, "Move step up", lambda: self.move_selected(-1), "Ctrl+Up")
        self._act(e, "Move step down", lambda: self.move_selected(+1), "Ctrl+Down")
        self._act(e, "Remove step", self.remove_selected, "Delete")
        self._act(e, "Skip / unskip step", self.toggle_skip)
        self._act(e, "Duplicate step", self.duplicate_selected, "Ctrl+D")
        self._act(e, "Insert save here", self.insert_save)
        r = self.add_menu("&Recipe")
        self._act(r, "Validate", self.validate, "F7")
        self._act(r, "View JSON", self.view_json, "Ctrl+J")
        self._act(r, "Reset to template", self.reset_to_template)
        self._act(r, "Edit inputs…", self.edit_inputs)
        r.addSeparator()
        self._act(r, "Queue recipe", self.queue_recipe, "Ctrl+Return")
        self.add_theme_menu()
        self.add_help_menu()

    @staticmethod
    def _act(menu, text, slot, shortcut=None):
        a = menu.addAction(text)
        a.triggered.connect(lambda _=False: slot())
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        return a

    def _fill_template_menu(self):
        self.template_menu.clear()
        names = sorted(self.catalog.templates)
        groups: dict[str, list[str]] = {}
        for n in names:
            prefix = n.split("_to_")[0] if "_to_" in n else "other"
            groups.setdefault(prefix, []).append(n)
        for prefix, items in groups.items():
            sub = self.template_menu.addMenu(prefix) if len(groups) > 1 else self.template_menu
            for n in items:
                a = sub.addAction(n.replace(".json", "") + ("  (user)" if self.catalog.is_user_template(n) else ""))
                a.triggered.connect(lambda _=False, name=n: self.load_template(name))

    # ------------------------------------------------------------------- body
    def _build_body(self):
        theme = theming.current_theme()
        gap = theme.density.gap
        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(gap, gap, gap, gap)
        outer.setSpacing(gap)
        self.setCentralWidget(central)

        # ---- catalog ---------------------------------------------------------
        cat_panel = Panel(layout="v")
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("search steps, arguments…")
        self.search.setClearButtonEnabled(True)
        cat_panel.layout().addWidget(self.search)
        self.catalog_filter = CatalogFilter()
        self.catalog_filter.setSourceModel(self.catalog_model)
        self.catalog_view = QtWidgets.QTreeView()
        self.catalog_view.setModel(self.catalog_filter)
        self.catalog_view.setHeaderHidden(True)
        self.catalog_view.setDragEnabled(True)
        self.catalog_view.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.catalog_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.catalog_view.setRootIsDecorated(True)
        self.catalog_view.expandAll()
        self.catalog_view.doubleClicked.connect(self._catalog_double_clicked)
        self.catalog_view.selectionModel().selectionChanged.connect(self._on_catalog_selected)
        self.search.textChanged.connect(self._on_search)
        cat_panel.layout().addWidget(self.catalog_view, 1)
        self.catalog_hint = QtWidgets.QLabel("Double-click or drag a step into the recipe. "
                                             "It lands where the shipped templates usually put it.")
        self.catalog_hint.setWordWrap(True)
        self.catalog_hint.setProperty("role", "hint")
        cat_panel.layout().addWidget(self.catalog_hint)
        add_btn = QtWidgets.QPushButton("Add to recipe")
        add_btn.clicked.connect(self.add_selected_catalog_steps)
        cat_panel.layout().addWidget(add_btn)
        self.catalog_label = SectionLabel("Step catalog")
        cat_section = self._section(self.catalog_label, cat_panel)

        # ---- recipe steps -----------------------------------------------------
        rec_panel = Panel(layout="v")
        head = QtWidgets.QGridLayout()
        head.addWidget(QtWidgets.QLabel("Name"), 0, 0)
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.editingFinished.connect(self._on_name_edited)
        head.addWidget(self.name_edit, 0, 1)
        head.addWidget(QtWidgets.QLabel("Level"), 0, 2)
        self.level_chip = Chip("?", "dim")
        head.addWidget(self.level_chip, 0, 3)
        head.addWidget(QtWidgets.QLabel("Template"), 1, 0)
        self.origin_label = ElidedLabel("none")
        head.addWidget(self.origin_label, 1, 1)
        self.start_template_btn = QtWidgets.QPushButton("Start from template…")
        self.start_template_btn.setToolTip("Replace the recipe with a shipped or user template (Ctrl+T)")
        self.start_template_btn.clicked.connect(self.start_from_template_dialog)
        head.addWidget(self.start_template_btn, 1, 2, 1, 2)
        head.setColumnStretch(1, 1)
        rec_panel.layout().addLayout(head)
        self.steps_view = RecipeStepsView()
        self.steps_view.setModel(self.steps_model)
        self.steps_view.selectionModel().selectionChanged.connect(self._on_step_selected)
        self.steps_view.rows_moved.connect(lambda rows: self._after_structural())
        self.steps_view.steps_dropped.connect(lambda row: self._after_structural())
        self.steps_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.steps_view.customContextMenuRequested.connect(self._steps_context_menu)
        rec_panel.layout().addWidget(self.steps_view, 1)
        brow = QtWidgets.QHBoxLayout()
        for text, slot, tip in (("▲", lambda: self.move_selected(-1), "Move up (Ctrl+Up)"),
                                ("▼", lambda: self.move_selected(+1), "Move down (Ctrl+Down)"),
                                ("Remove", self.remove_selected, "Remove selected steps (Delete)"),
                                ("Skip", self.toggle_skip, "Keep the step in the recipe but skip it when running"),
                                ("Duplicate", self.duplicate_selected, "Copy the step with its settings"),
                                ("Insert save", self.insert_save, "Insert a save step after the selection")):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, s=slot: s())
            brow.addWidget(b)
        brow.addStretch(1)
        rec_panel.layout().addLayout(brow)
        self.issues_label = QtWidgets.QLabel()
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rec_panel.layout().addWidget(self.issues_label)
        self.steps_label = SectionLabel("Recipe steps")
        rec_section = self._section(self.steps_label, rec_panel)

        # ---- inspector --------------------------------------------------------
        insp_panel = Panel(layout="v")
        self.step_title = QtWidgets.QLabel("Select a step")
        f = self.step_title.font()
        f.setBold(True)
        self.step_title.setFont(f)
        insp_panel.layout().addWidget(self.step_title)
        self.step_summary = QtWidgets.QLabel()
        self.step_summary.setWordWrap(True)
        insp_panel.layout().addWidget(self.step_summary)
        srow = QtWidgets.QHBoxLayout()
        self.doc_btn = QtWidgets.QPushButton("Full docstring")
        self.doc_btn.clicked.connect(self.show_docstring)
        self.src_label = ElidedLabel("")
        srow.addWidget(self.doc_btn)
        srow.addWidget(self.src_label, 1)
        insp_panel.layout().addLayout(srow)

        insp_panel.layout().addWidget(SectionLabel("Keyword arguments"))
        self.args_view = QtWidgets.QTableView()
        self.args_view.setModel(self.args_model)
        self.value_delegate = ValueDelegate(self.args_view)
        self.value_delegate.popout_requested.connect(self.edit_arg_in_window)
        self.args_view.setItemDelegateForColumn(1, self.value_delegate)
        self.args_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.args_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.args_view.verticalHeader().setVisible(False)
        self.args_view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
                                       | QtWidgets.QAbstractItemView.EditTrigger.SelectedClicked
                                       | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed)
        self.args_view.doubleClicked.connect(self._args_double_clicked)
        self.args_view.setToolTip("Double-click a description (or any column but Value) to read it in full")
        ah = self.args_view.horizontalHeader()
        ah.setStretchLastSection(True)
        ah.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        for c, w in ((0, 150), (1, 120), (2, 90), (3, 110)):
            ah.resizeSection(c, w)
        insp_panel.layout().addWidget(self.args_view, 2)
        arow = QtWidgets.QHBoxLayout()
        self.popout_btn = QtWidgets.QPushButton("Edit in window…")
        self.popout_btn.setToolTip("Multi-line editor for dicts, lists and long strings (also opens automatically for those)")
        self.popout_btn.clicked.connect(lambda: self.edit_arg_in_window(self.args_view.currentIndex()))
        arow.addWidget(self.popout_btn)
        self.reset_arg_btn = QtWidgets.QPushButton("Reset argument")
        self.reset_arg_btn.clicked.connect(self.reset_selected_arg)
        self.reset_all_btn = QtWidgets.QPushButton("Reset all to defaults")
        self.reset_all_btn.clicked.connect(self.args_model.reset_all)
        arow.addWidget(self.reset_arg_btn)
        arow.addWidget(self.reset_all_btn)
        arow.addStretch(1)
        insp_panel.layout().addLayout(arow)

        insp_panel.layout().addWidget(SectionLabel("Calibrations"))
        self.calib_box = QtWidgets.QWidget()
        self.calib_grid = QtWidgets.QGridLayout(self.calib_box)
        self.calib_grid.setContentsMargins(0, 0, 0, 0)
        insp_panel.layout().addWidget(self.calib_box)

        insp_panel.layout().addWidget(SectionLabel("Inputs and settings"))
        g = QtWidgets.QGridLayout()
        self.inputs_label = ElidedLabel("no inputs")
        g.addWidget(QtWidgets.QLabel("Inputs"), 0, 0)
        g.addWidget(self.inputs_label, 0, 1)
        ibox = QtWidgets.QHBoxLayout()
        ibox.setContentsMargins(0, 0, 0, 0)
        addb = QtWidgets.QPushButton("Add files…")
        addb.setToolTip("Append FITS files to the recipe's inputs (Ctrl+I)")
        addb.clicked.connect(self.add_input_files_dialog)
        ib = QtWidgets.QPushButton("Edit list…")
        ib.setToolTip("Review, add or remove input files")
        ib.clicked.connect(self.edit_inputs)
        ibox.addWidget(addb)
        ibox.addWidget(ib)
        g.addLayout(ibox, 0, 2)
        g.addWidget(QtWidgets.QLabel("Output dir"), 1, 0)
        self.outputdir_edit = QtWidgets.QLineEdit()
        self.outputdir_edit.editingFinished.connect(self._on_outputdir_edited)
        g.addWidget(self.outputdir_edit, 1, 1)
        ob = QtWidgets.QPushButton("Browse…")
        ob.clicked.connect(self.browse_outputdir)
        g.addWidget(ob, 1, 2)
        g.addWidget(QtWidgets.QLabel("drpconfig"), 2, 0)
        self.drpconfig_edit = QtWidgets.QLineEdit()
        self.drpconfig_edit.setPlaceholderText('{"track_individual_errors": false}')
        self.drpconfig_edit.editingFinished.connect(self._on_drpconfig_edited)
        g.addWidget(self.drpconfig_edit, 2, 1, 1, 2)
        self.ram_heavy_cb = QtWidgets.QCheckBox("ram_heavy")
        self.ram_heavy_cb.setToolTip("Read frames one at a time; data not loaded until needed")
        self.chunks_cb = QtWidgets.QCheckBox("process_in_chunks")
        self.chunks_cb.setToolTip("Process frames in chunks of corgidrp.chunk_size")
        for cb in (self.ram_heavy_cb, self.chunks_cb):
            cb.toggled.connect(self._on_flags_toggled)
        fl = QtWidgets.QHBoxLayout()
        fl.addWidget(self.ram_heavy_cb)
        fl.addWidget(self.chunks_cb)
        fl.addStretch(1)
        g.addLayout(fl, 3, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("Split into"), 4, 0)
        sl = QtWidgets.QHBoxLayout()
        sl.setContentsMargins(0, 0, 0, 0)
        self.max_files_spin = QtWidgets.QSpinBox()
        self.max_files_spin.setRange(0, 100000)
        self.max_files_spin.setSpecialValueText("no limit")
        self.max_files_spin.setToolTip("Maximum input files per recipe; more inputs than this are split into "
                                       "several recipes (part 1/n, 2/n, ...) that are queued together. 0 = no limit.")
        self.max_files_spin.setValue(self.split.max_files)
        self.max_files_spin.editingFinished.connect(self._on_split_changed)
        sl.addWidget(QtWidgets.QLabel("max files"))
        sl.addWidget(self.max_files_spin)
        sl.addSpacing(8)
        sl.addWidget(QtWidgets.QLabel("by"))
        self.split_keys_edit = QtWidgets.QLineEdit()
        self.split_keys_edit.setPlaceholderText("header keywords, e.g. VISITID, DPAMNAME")
        self.split_keys_edit.setClearButtonEnabled(True)
        self.split_keys_edit.setToolTip("Inputs whose headers differ in any of these keywords go into separate recipes.")
        self.split_keys_edit.setText(self.split.keywords_text)
        self.split_keys_edit.editingFinished.connect(self._on_split_changed)
        sl.addWidget(self.split_keys_edit, 1)
        g.addLayout(sl, 4, 1, 1, 2)
        self.split_label = ElidedLabel("")
        self.split_label.setProperty("role", "hint")
        g.addWidget(self.split_label, 5, 1, 1, 2)
        g.setColumnStretch(1, 1)
        insp_panel.layout().addLayout(g)
        self.inspector_label = SectionLabel("Inspector")
        insp_section = self._section(self.inspector_label, insp_panel)

        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        for w in (cat_section, rec_section, insp_section):
            split.addWidget(w)
        split.setSizes([280, 420, 600])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        # ---- bottom bar ---------------------------------------------------------
        bar = QtWidgets.QHBoxLayout()
        for text, slot in (("Validate", self.validate), ("View JSON", self.view_json), ("Reset to template", self.reset_to_template)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: s())
            bar.addWidget(b)
        bar.addStretch(1)
        b = QtWidgets.QPushButton("Save as user template…")
        b.clicked.connect(self.save_as_template_dialog)
        bar.addWidget(b)
        b = QtWidgets.QPushButton("Save recipe…")
        b.clicked.connect(self.save_recipe_dialog)
        bar.addWidget(b)
        self.queue_btn = QtWidgets.QPushButton("Save && queue")
        self.queue_btn.setProperty("role", "primary")
        self.queue_btn.clicked.connect(self.queue_recipe)
        bar.addWidget(self.queue_btn)
        outer.addLayout(bar)

        self.status_chip = Chip("new", "dim")
        self.statusBar().addPermanentWidget(self.status_chip)

    @staticmethod
    def _section(label, body):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(body, 1)
        return w

    # ------------------------------------------------------------- recipe I/O
    @property
    def recipe(self) -> Recipe:
        return self.steps_model.recipe

    def new_recipe(self):
        if not self._confirm_discard():
            return
        self._template_origin = None
        self._template_origin_name = ""
        self._set_recipe(Recipe(name="new_recipe"))

    def start_from_template_dialog(self):
        """Searchable template chooser; loads the chosen template as the new recipe."""
        current = self._template_origin_name.split("  ")[0] if self._template_origin_name else None
        dlg = TemplateChooserDialog(self.catalog, self, current=current)
        try:
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            name = dlg.selected()
        finally:
            dlg.deleteLater()
        if name:
            self.load_template(name)

    def load_template(self, name: str):
        if not self._confirm_discard():
            return
        tpl = self.catalog.templates.get(name)
        if tpl is None:
            return
        r = Recipe.from_dict(tpl, source_path=self.catalog.template_paths.get(name))
        r.template = False
        self._template_origin = Recipe.from_dict(tpl)
        self._template_origin_name = name + ("  (user template)" if self.catalog.is_user_template(name) else "")
        self._set_recipe(r)
        self.statusBar().showMessage(f"Loaded template {name}", 5000)

    def load_recipe(self, recipe: Recipe, origin: Recipe | None = None):
        """Used by the Parser: edit a populated recipe."""
        if not self._confirm_discard():
            return
        self._template_origin = origin
        self._template_origin_name = origin.name if origin else ""
        src = recipe.extra.get("RECIPE_SRC")
        if origin is None and src and os.path.exists(str(src)):
            try:
                self._template_origin = Recipe.load(str(src))
                self._template_origin_name = os.path.basename(str(src))
            except (OSError, ValueError):
                pass
        self._set_recipe(recipe.copy())
        self.show()
        self.raise_()
        self.activateWindow()

    def open_recipe_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open recipe", self._last_dir, "Recipe JSON (*.json)")
        if path:
            self._last_dir = os.path.dirname(path)
            try:
                r = Recipe.load(path)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, "Open recipe", f"Could not read {path}:\n{exc}")
                return
            self.load_recipe(r)

    def _set_recipe(self, recipe: Recipe):
        self._undo.clear()
        self.steps_model.set_recipe(recipe)
        self._dirty = False
        self._refresh_all()
        if recipe.steps:
            self.steps_view.setCurrentIndex(self.steps_model.index(0))

    def save_recipe_dialog(self):
        start = os.path.join(self.recipe.outputdir or self._last_dir, f"{self.recipe.name}.json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save recipe", start, "Recipe JSON (*.json)")
        if path:
            self.save_recipe(path)

    def save_recipe(self, path: str) -> list[str]:
        """Write the recipe; with an active split, one file per batch (``<stem>_<batch>.json``)."""
        recipes = self.split_recipes()
        written = []
        if len(recipes) == 1:
            self.recipe.save(path)
            written.append(path)
        else:
            stem, ext = os.path.splitext(path)
            for r in recipes:
                suffix = r.name[len(self.recipe.name):] if r.name.startswith(self.recipe.name) else "_" + r.name
                p = f"{stem}{suffix}{ext or '.json'}"
                r.save(p)
                written.append(p)
        self._last_dir = os.path.dirname(path)
        self._dirty = False
        self._refresh_status()
        if len(written) == 1:
            self.statusBar().showMessage(f"Saved {path}", 6000)
        else:
            self.statusBar().showMessage(f"Saved {len(written)} recipes: {os.path.basename(written[0])} … {os.path.basename(written[-1])}", 8000)
        return written

    def save_as_template_dialog(self):
        import corgidrp
        folder = getattr(corgidrp, "user_templates_dir", None) or os.path.join(self.backend.home(), ".corgidrp", "user_templates")
        name, ok = QtWidgets.QInputDialog.getText(self, "Save as user template", f"Template filename in\n{folder}:",
                                                  text=f"{self.recipe.name}.json")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not name.endswith(".json"):
            name += ".json"
        path = os.path.join(folder, name)
        if self.backend.exists(path):
            r = QtWidgets.QMessageBox.question(self, "Overwrite?", f"{name} already exists in your user templates. Overwrite it?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        shipped = os.path.join(os.path.dirname(self.catalog.template_paths.get("l1_to_l2b.json", "")), name)
        note = ""
        if name in self.catalog.templates and not self.catalog.is_user_template(name):
            note = f"\nNote: {name} shadows a shipped template; the walker will use yours (enforce_template_structure may apply)."
        self.save_as_template(path)
        QtWidgets.QMessageBox.information(self, "Template saved", f"Saved {path}{note}\n\nRebuild the step catalog (Launcher → Tools) to see it in the template lists.")

    def save_as_template(self, path: str):
        self.backend.makedirs(os.path.dirname(path))
        t = self.recipe.as_template(os.path.splitext(os.path.basename(path))[0])
        t.save(path)
        self.statusBar().showMessage(f"Saved template {path}", 6000)
        return path

    def reload_catalog(self, catalog: StepCatalog | None = None):
        self.catalog = catalog or catalog_cache.load_catalog()
        self.steps_model.catalog = self.catalog
        self.catalog_model.set_catalog(self.catalog)
        self.catalog_view.expandAll()
        self._fill_template_menu()
        self._refresh_all()

    # ------------------------------------------------------------------ edits
    def _snapshot(self):
        self._undo.append(self.recipe.to_dict())
        del self._undo[:-100]

    def undo(self):
        if not self._undo:
            return
        d = self._undo.pop()
        r = Recipe.from_dict(d, self.recipe.source_path)
        self.steps_model.set_recipe(r)
        self._refresh_all()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.steps_view.selectionModel().selectedIndexes()})

    def _select(self, rows):
        self.steps_view._select_rows(rows)

    def add_step(self, name: str, row: int | None = None) -> int:
        self._snapshot()
        new_row, reason = self.steps_model.insert_new(name, row)
        self._select([new_row])
        self.statusBar().showMessage(f"Inserted {name} at {new_row + 1}" + (f": {reason}" if reason else ""), 6000)
        return new_row

    def add_selected_catalog_steps(self):
        for idx in self.catalog_view.selectionModel().selectedIndexes():
            info = idx.data(StepInfoRole)
            if info is not None:
                self.add_step(info.name)

    def _catalog_double_clicked(self, index):
        info = index.data(StepInfoRole)
        if info is not None:
            self.add_step(info.name)

    def move_selected(self, delta: int):
        rows = self._selected_rows()
        if not rows:
            return
        n = self.steps_model.rowCount()
        if (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] >= n - 1):
            return
        self._snapshot()
        dest = rows[0] - 1 if delta < 0 else rows[-1] + 2
        new_rows = self.steps_model.move_rows(rows, dest)
        self._select(new_rows)

    def remove_selected(self):
        rows = self._selected_rows()
        if rows:
            self._snapshot()
            self.steps_model.remove_rows_list(rows)
            self._select([min(rows[0], self.steps_model.rowCount() - 1)] if self.steps_model.rowCount() else [])

    def toggle_skip(self):
        rows = self._selected_rows()
        if rows:
            self._snapshot()
            self.steps_model.toggle_skip(rows)
            self._on_step_selected()

    def duplicate_selected(self):
        rows = self._selected_rows()
        if rows:
            self._snapshot()
            for i, r in enumerate(rows):
                self.steps_model.insert_step(rows[-1] + 1 + i, self.steps_model.step(r).copy())
            self._select(list(range(rows[-1] + 1, rows[-1] + 1 + len(rows))))

    def insert_save(self):
        rows = self._selected_rows()
        row = (rows[-1] + 1) if rows else self.steps_model.rowCount()
        self._snapshot()
        self.steps_model.insert_step(row, RecipeStep(SAVE_STEP_NAME))
        self._select([row])

    def _steps_context_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        for text, slot in (("Move up", lambda: self.move_selected(-1)), ("Move down", lambda: self.move_selected(+1)),
                           ("Remove", self.remove_selected), ("Skip / unskip", self.toggle_skip),
                           ("Duplicate", self.duplicate_selected), ("Insert save after", self.insert_save)):
            menu.addAction(text).triggered.connect(lambda _=False, s=slot: s())
        menu.exec(self.steps_view.viewport().mapToGlobal(pos))

    def _after_structural(self):
        self._dirty = True
        self._refresh_all()

    def _on_recipe_changed(self):
        self._dirty = True
        self._refresh_status()
        self._refresh_issues()

    def _on_arg_changed(self, _name):
        rows = self._selected_rows()
        if rows:
            self.steps_model.notify_step_changed(rows[0])

    def _on_name_edited(self):
        name = self.name_edit.text().strip()
        if name and name != self.recipe.name:
            self._snapshot()
            self.recipe.name = name
            self._on_recipe_changed()

    def _on_outputdir_edited(self):
        v = self.outputdir_edit.text().strip()
        if v != self.recipe.outputdir:
            self.recipe.outputdir = v
            self._on_recipe_changed()

    def _on_drpconfig_edited(self):
        text = self.drpconfig_edit.text().strip() or "{}"
        try:
            d = json.loads(text)
            if not isinstance(d, dict):
                raise ValueError("drpconfig must be a JSON object")
        except ValueError as exc:
            self.statusBar().showMessage(f"drpconfig not applied: {exc}", 6000)
            return
        if d != self.recipe.drpconfig:
            self.recipe.drpconfig = d
            self._on_recipe_changed()

    def _on_flags_toggled(self, _checked):
        for key, cb in (("ram_heavy", self.ram_heavy_cb), ("process_in_chunks", self.chunks_cb)):
            if cb.isChecked():
                self.recipe.extra[key] = True
            else:
                self.recipe.extra.pop(key, None)
        self._on_recipe_changed()

    def browse_outputdir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Output directory", self.recipe.outputdir or self._last_dir)
        if d:
            self.outputdir_edit.setText(d)
            self._on_outputdir_edited()

    def add_input_files_dialog(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add input files", self._last_dir,
                                                          "FITS files (*.fits *.fit *.fits.gz);;All files (*)")
        if paths:
            self._last_dir = os.path.dirname(paths[0])
            self.add_inputs(paths)

    def add_inputs(self, paths: list[str]):
        new = [p for p in paths if p not in self.recipe.inputs]
        if not new:
            return
        self._snapshot()
        self.recipe.inputs.extend(new)
        self._on_recipe_changed()
        self._refresh_header()
        self.statusBar().showMessage(f"Added {len(new)} input file{'s' if len(new) != 1 else ''}; {len(self.recipe.inputs)} total", 6000)

    # ---------------------------------------------------------------- splitting
    def _on_split_changed(self):
        spec = SplitSpec(max_files=self.max_files_spin.value(),
                         keywords=SplitSpec.parse_keywords(self.split_keys_edit.text()))
        self.split_keys_edit.setText(spec.keywords_text)
        if spec != self.split:
            self.split = spec
            self._refresh_split_label()

    def set_split(self, max_files: int = 0, keywords: str | list[str] = ""):
        self.max_files_spin.setValue(max_files)
        self.split_keys_edit.setText(keywords if isinstance(keywords, str) else ", ".join(keywords))
        self._on_split_changed()

    def _headers_of(self, path: str):
        h = self._headers_cache.get(path)
        if h is None:
            h = self.backend.read_headers(path)
            self._headers_cache[path] = h
        return h

    def split_batches(self):
        """The input batches the current split rules produce (one when nothing is split)."""
        if not self.recipe.inputs:
            return []
        return split_paths(self.recipe.inputs, self.split, self._headers_of if self.split.keywords else None)

    def split_recipes(self) -> list[Recipe]:
        """Copies of the recipe, one per input batch, named ``<name>_<batch>``."""
        batches = self.split_batches()
        if len(batches) <= 1:
            return [self.recipe.copy()]
        out = []
        for b in batches:
            r = self.recipe.copy()
            r.inputs = list(b.items)
            r.name = batch_recipe_name(self.recipe.name, b)
            out.append(r)
        return out

    def _refresh_split_label(self):
        if not self.split.active or not self.recipe.inputs:
            self.split_label.setFullText("")
            self.split_label.setToolTip("")
            return
        batches = self.split_batches()
        if len(batches) <= 1:
            self.split_label.setFullText(f"{self.split.describe()}: all {len(self.recipe.inputs)} files fit in one recipe")
            self.split_label.setToolTip("")
            return
        parts = [f"{b.label} ({len(b.items)})" for b in batches]
        self.split_label.setFullText(f"→ {len(batches)} recipes: " + ", ".join(parts))
        self.split_label.setToolTip("\n".join(f"{batch_recipe_name(self.recipe.name, b)}: {len(b.items)} files" for b in batches))

    def edit_inputs(self):
        dlg = InputsDialog(self.recipe.inputs, self, self._last_dir)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._snapshot()
            self.recipe.inputs = dlg.inputs()
            self._on_recipe_changed()
            self._refresh_header()

    # ---------------------------------------------------------------- inspector
    def _on_step_selected(self, *_):
        rows = self._selected_rows()
        step = self.steps_model.step(rows[0]) if rows else None
        info = self.steps_model.info(rows[0]) if rows else None
        self.args_model.set_step(step, info)
        if step is None:
            self.step_title.setText("Select a step")
            self.step_summary.clear()
            self.src_label.setFullText("")
        else:
            title = f"{rows[0] + 1}. {step.name}"
            if len(rows) > 1:
                title += f"  (+{len(rows) - 1} more selected)"
            if step.skip:
                title += "  — skipped"
            self.step_title.setText(title)
            if info:
                self.step_summary.setText(info.summary + (f"\n\nUsed on {', '.join(info.levels)} data in {info.n_templates} templates." if info.levels else ""))
                self.src_label.setFullText(f"{info.qualname}  ({os.path.basename(info.source_file)}:{info.source_line})" if info.source_file else info.qualname)
            else:
                self.step_summary.setText("This step name is not in the catalog. Rebuild the catalog if corgidrp changed, or remove the step.")
                self.src_label.setFullText("")
        self.doc_btn.setEnabled(info is not None)
        self._rebuild_calib_rows(step, info)

    def _rebuild_calib_rows(self, step, info):
        while self.calib_grid.count():
            item = self.calib_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        if step is None:
            return
        slots = info.calibs if info else []
        existing = list((step.calibs or {}).items())
        if not slots and not existing:
            lab = QtWidgets.QLabel("This step takes no calibration products.")
            lab.setProperty("role", "hint")
            self.calib_grid.addWidget(lab, 0, 0, 1, 3)
            return
        n = max(len(slots), len(existing))
        for j in range(n):
            slot = slots[j] if j < len(slots) else None
            label, spec = existing[j] if j < len(existing) else (None, None)
            lab_text = f"{j + 1}. {slot.param}" if slot else f"{j + 1}. (extra)"
            self.calib_grid.addWidget(QtWidgets.QLabel(lab_text), j, 0)
            type_cb = QtWidgets.QComboBox()
            choices = list(slot.datatypes) if slot else []
            if label and label not in choices:
                choices.insert(0, label)
            choices.append("(unset)")
            type_cb.addItems(choices)
            type_cb.setCurrentText(label if label else "(unset)")
            type_cb.setToolTip((slot.description or slot.type_str) if slot else "not a known slot of this step")
            self.calib_grid.addWidget(type_cb, j, 1)
            spec_cb = QtWidgets.QComboBox()
            spec_cb.addItems(_SPEC_CHOICES)
            spec_cb.setEditable(True)
            spec_cb.setCurrentText(self._spec_display(spec))
            spec_cb.setEnabled(bool(label))
            spec_cb.setToolTip("AUTOMATIC lets the CalDB pick; ', OPTIONAL' lets the step run without it; or a file path")
            self.calib_grid.addWidget(spec_cb, j, 2)
            type_cb.currentTextChanged.connect(lambda text, jj=j: self._on_calib_type(jj, text))
            spec_cb.activated.connect(lambda _i, jj=j, cb=spec_cb: self._on_calib_spec(jj, cb))
            spec_cb.lineEdit().editingFinished.connect(lambda jj=j, cb=spec_cb: self._on_calib_spec(jj, cb))
        self.calib_grid.setColumnStretch(2, 1)

    @staticmethod
    def _spec_display(spec) -> str:
        if spec is None:
            return _SPEC_CHOICES[2]
        return str(spec)

    def _calib_items(self, step) -> list[tuple[str, object]]:
        return list((step.calibs or {}).items())

    def _set_calib_items(self, step, items):
        step.calibs = {k: v for k, v in items if k} or None

    def _on_calib_type(self, j: int, text: str):
        rows = self._selected_rows()
        if not rows:
            return
        step = self.steps_model.step(rows[0])
        items = self._calib_items(step)
        while len(items) <= j:
            items.append(("", None))
        old_label, old_spec = items[j]
        if text == "(unset)":
            items[j] = ("", None)
        else:
            items[j] = (text, old_spec if old_label else "AUTOMATIC")
        self._snapshot()
        self._set_calib_items(step, [it for it in items if it[0]] if text == "(unset)" and j == len(items) - 1 else items)
        # keys must stay ordered: drop trailing unset slots
        self.steps_model.notify_step_changed(rows[0])
        self._on_step_selected()

    def _on_calib_spec(self, j: int, cb: QtWidgets.QComboBox):
        rows = self._selected_rows()
        if not rows:
            return
        step = self.steps_model.step(rows[0])
        items = self._calib_items(step)
        if j >= len(items):
            return
        text = cb.currentText().strip()
        label, old = items[j]
        if text == _SPEC_CHOICES[3]:
            from .dialogs import CalibChooserDialog
            dlg = CalibChooserDialog(label, self)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dlg.path:
                cb.setCurrentText(self._spec_display(old))
                return
            text = dlg.path
        new = None if text.startswith("None") else text
        if new == old:
            return
        self._snapshot()
        items[j] = (label, new)
        self._set_calib_items(step, items)
        self.steps_model.notify_step_changed(rows[0])
        cb.setCurrentText(self._spec_display(new))

    def edit_arg_in_window(self, index):
        """Pop-out editor for the argument at ``index`` (any column of its row)."""
        if index is None or not index.isValid() or self.args_model.step is None:
            return
        from .dialogs import ValueEditorDialog
        spec = self.args_model.rows[index.row()]
        value, _explicit = self.args_model.current_value(spec)
        dlg = ValueEditorDialog(spec.name, value, spec.default, spec.type_str, spec.description, self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self._snapshot()
        self.args_model.setData(self.args_model.index(index.row(), 1), dlg.text(), Qt.ItemDataRole.EditRole)

    def _args_double_clicked(self, index):
        # Column 1 (Value) starts the in-place editor; every other column opens the
        # read-only description so long docstring text is not confined to the cell.
        if index.isValid() and index.column() != 1:
            self.show_arg_description(index)

    def arg_description_text(self, index) -> str:
        """Full text for the argument at ``index``: name, type, default, current value, description."""
        spec = self.args_model.rows[index.row()]
        value, explicit = self.args_model.current_value(spec)
        lines = [spec.name + (f"  ({spec.type_str})" if spec.type_str else ""), ""]
        if spec.default is MISSING:
            lines.append("Default: (required)")
        else:
            lines.append(f"Default: {format_value(spec.default)}")
        if explicit:
            lines.append(f"In this recipe: {format_value(value)}")
        lines.append("")
        lines.append(spec.description or "(no description in the docstring)")
        return "\n".join(lines)

    def show_arg_description(self, index=None):
        """Read-only pop-out with the full description of the argument at ``index``."""
        if index is None:
            index = self.args_view.currentIndex()
        if index is None or not index.isValid() or not self.args_model.rows:
            return None
        spec = self.args_model.rows[index.row()]
        return text_dialog(self, f"Argument: {spec.name}", self.arg_description_text(index), mono=False)

    def reset_selected_arg(self):
        idx = self.args_view.currentIndex()
        if idx.isValid():
            self.args_model.reset_row(idx.row())

    def show_docstring(self):
        rows = self._selected_rows()
        info = self.steps_model.info(rows[0]) if rows else None
        if info:
            text_dialog(self, info.qualname, info.description)

    def _on_catalog_selected(self, *_):
        idxs = self.catalog_view.selectionModel().selectedIndexes()
        info = idxs[0].data(StepInfoRole) if idxs else None
        if info is None:
            self.catalog_hint.setText("Double-click or drag a step into the recipe. It lands where the shipped templates usually put it.")
            return
        pos, reason = self.catalog.suggest_index(self.recipe.step_names, info.name)
        self.catalog_hint.setText(f"{info.summary}\n\nWould be inserted at position {pos + 1}: {reason}.")

    def _on_search(self, text):
        self.catalog_filter.setFilterRegularExpression(QtCore.QRegularExpression.escape(text) if text else "")
        self.catalog_view.expandAll()

    # -------------------------------------------------------------- validation
    def current_issues(self) -> list[Issue]:
        return validate_recipe(self.recipe, self.catalog, self.start_level(), check_inputs=False)

    def start_level(self) -> str | None:
        return guess_start_level(self.recipe, self.backend)

    def validate(self):
        issues = validate_recipe(self.recipe, self.catalog, self.start_level(), check_inputs=True)
        self.steps_model.set_issues(issues)
        if not issues:
            QtWidgets.QMessageBox.information(self, "Validate", "No problems found.")
            return
        text = "\n".join(str(i) for i in issues)
        text_dialog(self, f"Validation: {sum(i.severity == 'error' for i in issues)} errors, {sum(i.severity == 'warning' for i in issues)} warnings", text, mono=False)

    def view_json(self):
        recipes = self.split_recipes()
        title = f"{self.recipe.name} — JSON" + (f" ({len(recipes)} recipes after splitting)" if len(recipes) > 1 else "")
        RecipePreviewDialog([[r] for r in recipes], self, title=title).open()

    def reset_to_template(self):
        if self._template_origin is None:
            QtWidgets.QMessageBox.information(self, "Reset to template", "This recipe was not loaded from a template.")
            return
        self._snapshot()
        r = self._template_origin.copy()
        r.template = False
        r.inputs = list(self.recipe.inputs)
        r.outputdir = self.recipe.outputdir
        self.steps_model.set_recipe(r)
        self._refresh_all()

    def queue_recipe(self):
        issues = self.current_issues()
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            self.steps_model.set_issues(issues)
            QtWidgets.QMessageBox.warning(self, "Cannot queue", "Fix these first:\n" + "\n".join(str(e) for e in errors[:8]))
            return
        if not self.recipe.inputs:
            QtWidgets.QMessageBox.warning(self, "No inputs", "Add input files first (Inputs → Edit…).")
            return
        if not self.recipe.outputdir:
            QtWidgets.QMessageBox.warning(self, "Output directory", "Set an output directory first.")
            return
        recipes = self.split_recipes()
        req = QueueRequest(chains=[[r] for r in recipes], outputdir=self.recipe.outputdir, source="builder")
        handled = bool(self.queue_handler(req)) if self.queue_handler else False
        self.recipes_queued.emit(req)
        if not handled and self.queue_handler is None:
            written = dump_recipes(req, backend=self.backend)
            self.statusBar().showMessage(f"No Status Console attached; wrote {len(written)} recipe file{'s' if len(written) != 1 else ''} to {os.path.dirname(written[0])}", 8000)
        elif len(recipes) > 1:
            self.statusBar().showMessage(f"Queued {len(recipes)} recipes ({self.split.describe()})", 8000)
        self._dirty = False
        self._refresh_status()

    # ----------------------------------------------------------------- refresh
    def _refresh_all(self):
        self._refresh_header()
        self._refresh_status()
        self._refresh_issues()
        self._on_step_selected()

    def _refresh_header(self):
        r = self.recipe
        self.name_edit.setText(r.name)
        self.outputdir_edit.setText(r.outputdir)
        self.drpconfig_edit.setText(json.dumps(r.drpconfig) if r.drpconfig else "")
        for key, cb in (("ram_heavy", self.ram_heavy_cb), ("process_in_chunks", self.chunks_cb)):
            cb.blockSignals(True)
            cb.setChecked(bool(r.extra.get(key)))
            cb.blockSignals(False)
        n = len(r.inputs)
        self.inputs_label.setFullText(f"{n} file{'s' if n != 1 else ''}" + (f": {os.path.basename(r.inputs[0])} … {os.path.basename(r.inputs[-1])}" if n else ""))
        self._refresh_split_label()
        origin = self._template_origin_name or (os.path.basename(r.source_path) if r.source_path else "none")
        self.origin_label.setFullText(origin)
        lvl = self.start_level()
        self.level_chip.setText(lvl or "?")
        self.level_chip.set_kind("run" if lvl else "dim")
        self.steps_label.setRawText(f"Recipe steps · {len(r.steps)}")

    def _refresh_status(self):
        self.status_chip.setText("modified" if self._dirty else "saved")
        self.status_chip.set_kind("warn" if self._dirty else "ok")
        self.setWindowTitle(f"Recipe Builder — {self.recipe.name}{'*' if self._dirty else ''}")
        self.steps_label.setRawText(f"Recipe steps · {len(self.recipe.steps)}")

    def _refresh_issues(self):
        issues = self.current_issues()
        self.steps_model.set_issues(issues)
        errs = [i for i in issues if i.severity == "error"]
        warns = [i for i in issues if i.severity == "warning"]
        pal = theming.current_theme().palette
        if not self.recipe.steps:
            self.issues_label.setText("Empty recipe. Drag steps in from the catalog, or click Start from template… (Ctrl+T).")
            self.issues_label.setStyleSheet(f"color: {pal.muted}")
        elif not issues:
            self.issues_label.setText("No problems found.")
            self.issues_label.setStyleSheet(f"color: {pal.ok}")
        else:
            first = errs[0] if errs else warns[0]
            more = len(issues) - 1
            self.issues_label.setText(f"{len(errs)} error{'s' if len(errs) != 1 else ''}, {len(warns)} warning{'s' if len(warns) != 1 else ''}. "
                                      f"{first}" + (f"  (+{more} more, see Validate)" if more else ""))
            self.issues_label.setStyleSheet(f"color: {pal.err if errs else pal.warn}")

    def theme_changed(self, theme):
        self._refresh_issues()
        self.steps_model.layoutChanged.emit()
        self.args_model.layoutChanged.emit()

    # --------------------------------------------------------------- lifecycle
    def _confirm_discard(self) -> bool:
        if not self._dirty or not self.recipe.steps:
            return True
        r = QtWidgets.QMessageBox.question(self, "Discard changes?", f"'{self.recipe.name}' has unsaved changes. Discard them?")
        return r == QtWidgets.QMessageBox.StandardButton.Yes

    def closeEvent(self, event):
        self.settings.setValue("builder/last_dir", self._last_dir)
        self.settings.setValue("builder/split_max_files", self.split.max_files)
        self.settings.setValue("builder/split_keywords", self.split.keywords_text)
        self.settings.setValue("builder/geometry", self.saveGeometry())
        super().closeEvent(event)
