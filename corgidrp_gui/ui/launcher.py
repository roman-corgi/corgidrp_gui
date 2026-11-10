"""The Launcher: entry point to the four tools plus a live view of corgidrp's configuration."""

from __future__ import annotations

import csv
import os
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from .. import theming
from .base import ThemedWindow
from .widgets.themed import ElidedLabel, Panel, SectionLabel, TileButton

CONFIG_FILE = Path.home() / ".corgidrp" / "corgidrp.cfg"
REBUILD_TEXT = "Rebuild step catalog\u2026"
FLAG_KEYS = ("jit_calib_id", "skip_missing_cal_steps", "enforce_template_structure")

# (key, title, subtitle, phase that delivers it)
TOOLS = [
    ("parser", "Recipe Parser", "files → recommended recipes", 2),
    ("builder", "Recipe Builder", "compose and edit recipes", 3),
    ("console", "Status Console", "queue, progress, logs", 4),
    ("caldb", "Calibration DB", "browse, rescan, test matches", 5),
    ("dataset", "Dataset Summary", "what is in a pile of files", 7),
]


def _backend():
    from ..backend import default_backend
    return default_backend()


def _csv_rows(path: str) -> int | None:
    # The Backend interface has no text-read method yet, so the CSV is read directly
    # (local caldb only).  Move this behind the backend when it grows one.
    try:
        with open(path, newline="") as fh:
            n = sum(1 for _ in csv.reader(fh))
        return max(n - 1, 0)            # minus the header row
    except OSError:
        return None


def _count(directory, pattern: str) -> int | None:
    try:
        return sum(1 for e in _backend().listdir(str(directory), pattern) if not e.is_dir)
    except (OSError, TypeError):
        return None


def corgidrp_config() -> list[tuple[str, str, str]]:
    """Live configuration rows ``(key, value, tooltip)`` read from corgidrp.

    Raises whatever ``import corgidrp`` raises; the caller shows the message.
    """
    import corgidrp

    be = _backend()
    rows: list[tuple[str, str, str]] = []
    pkg = os.path.dirname(getattr(corgidrp, "__file__", "") or "")
    rows.append(("corgidrp", f"{getattr(corgidrp, '__version__', '?')}  ·  {pkg}", pkg))

    caldb = getattr(corgidrp, "caldb_filepath", None)
    if caldb:
        n = _csv_rows(caldb) if be.exists(caldb) else None
        extra = f"{n} rows" if n is not None else "not created yet"
        rows.append(("caldb", f"{extra}  ·  {caldb}", caldb))
    else:
        rows.append(("caldb", "not configured", ""))

    tdir = getattr(corgidrp, "user_templates_dir", None)
    if tdir:
        n = _count(tdir, "*.json") if be.isdir(tdir) else None
        extra = f"{n} .json" if n is not None else "missing"
        rows.append(("user templates", f"{extra}  ·  {tdir}", tdir))
    else:
        rows.append(("user templates", "not configured", ""))

    for flag in ("jit_calib_id", "skip_missing_cal_steps", "enforce_template_structure"):
        rows.append((flag, str(getattr(corgidrp, flag, "n/a")), f"corgidrp.{flag}"))

    try:
        from corgidrp import walker
        nsteps = len(getattr(walker, "all_steps", {}))
        rdir = getattr(walker, "recipe_dir", None)
        ntpl = _count(rdir, "*.json") if rdir else None
        rows.append(("walker", f"{nsteps} steps  ·  {ntpl} templates", str(rdir or "")))
    except Exception as exc:  # walker import pulls in heavy deps; report rather than fail
        rows.append(("walker", f"not importable: {exc}", ""))
    return rows


class _Worker(QtCore.QThread):
    """Run ``fn()`` off the GUI thread; emits ``done(result)`` or ``failed(message)``."""

    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:  # reported in the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


def _catalog_api():
    """The step-catalog cache API (imported lazily; it lives in corgidrp_gui.core)."""
    from ..core import catalog_cache
    return catalog_cache


class LauncherWindow(ThemedWindow):
    """Four tool tiles, a configuration panel and theme controls.

    The step catalog is never rebuilt automatically: the status line is read from the
    cache file at startup (instant) and the user rebuilds explicitly.
    """

    tool_requested = QtCore.Signal(str)
    catalog_rebuilt = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent, title="corgidrp GUI")
        self.setObjectName("LauncherWindow")
        self._gallery = None
        self._handlers: dict[str, object] = {}
        self._workers: set[_Worker] = set()
        self._rebuild_actions: list = []
        self._build_menus()
        self._build_body()
        self.tool_requested.connect(self._default_tool_handler)
        self.resize(560, 520)

    # ---------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        file_menu = self.add_menu("&File")
        act = file_menu.addAction("Open config file")
        act.triggered.connect(self.open_config_file)
        file_menu.addSeparator()
        quit_act = file_menu.addAction("Quit")
        quit_act.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)

        tools_menu = self.add_menu("&Tools")
        for key, title, _sub, _phase in TOOLS:
            a = tools_menu.addAction(title)
            a.triggered.connect(lambda _=False, k=key: self.tool_requested.emit(k))
        tools_menu.addSeparator()
        rb = tools_menu.addAction(REBUILD_TEXT)
        rb.triggered.connect(self.rebuild_catalog)
        self._rebuild_actions.append(rb)
        g = tools_menu.addAction("Theme gallery")
        g.triggered.connect(self.open_gallery)

        self.add_theme_menu()
        self.add_help_menu()

    # ---------------------------------------------------------------- body
    def _build_body(self) -> None:
        theme = theming.current_theme()
        central = QtWidgets.QWidget()
        central.setObjectName("central")
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # title band
        band = QtWidgets.QFrame()
        band.setProperty("role", "titlebar")
        bl = QtWidgets.QHBoxLayout(band)
        title = QtWidgets.QLabel("corgidrp GUI")
        title.setProperty("role", "title")
        sub = QtWidgets.QLabel("Roman Coronagraph data reduction")
        sub.setProperty("role", "subtitle")
        bl.addWidget(title)
        bl.addStretch(1)
        bl.addWidget(sub, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        self._band_layout = bl
        outer.addWidget(band)

        body = QtWidgets.QWidget()
        self._body_layout = QtWidgets.QVBoxLayout(body)
        outer.addWidget(body, 1)

        # tool tiles
        grid = QtWidgets.QGridLayout()
        self._grid = grid
        self.tiles: dict[str, TileButton] = {}
        for i, (key, title_text, subtitle, _phase) in enumerate(TOOLS):
            tile = TileButton(title_text, subtitle, primary=(key == "parser"))
            tile.setObjectName(f"tile_{key}")
            tile.clicked.connect(lambda _=False, k=key: self.tool_requested.emit(k))
            grid.addWidget(tile, i // 2, i % 2)
            self.tiles[key] = tile
        self._body_layout.addLayout(grid, 1)     # tiles take any spare height

        # configuration
        self._body_layout.addSpacing(4)
        self.config_label = SectionLabel("Configuration")
        self._body_layout.addWidget(self.config_label)
        self.config_panel = Panel(layout="grid")
        self.config_panel.setObjectName("configPanel")
        self._body_layout.addWidget(self.config_panel)
        self.catalog_value = QtWidgets.QLabel("")
        self.catalog_value.setObjectName("catalogStatus")
        self.catalog_value.setWordWrap(True)
        self.catalog_value.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.catalog_check = QtWidgets.QPushButton("Check for changes")
        self.catalog_check.setProperty("role", "link")
        self.catalog_check.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.catalog_check.setToolTip("Compare the cached catalog with the installed corgidrp "
                                      "(imports corgidrp; nothing is rebuilt)")
        self.catalog_check.clicked.connect(self.check_catalog)
        self.refresh_config()
        self.refresh_catalog_status()
        self.config_panel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                        QtWidgets.QSizePolicy.Policy.Maximum)

        cfg_row = QtWidgets.QHBoxLayout()
        cfg_row.addWidget(self.catalog_check)
        cfg_row.addStretch(1)
        self.rebuild_button = QtWidgets.QPushButton(REBUILD_TEXT)
        self.rebuild_button.setToolTip("Re-introspect corgidrp and overwrite the cached step catalog")
        self.rebuild_button.clicked.connect(self.rebuild_catalog)
        cfg_row.addWidget(self.rebuild_button)
        cfg_btn = QtWidgets.QPushButton("Open config file")
        cfg_btn.setToolTip(str(CONFIG_FILE))
        cfg_btn.clicked.connect(self.open_config_file)
        cfg_row.addWidget(cfg_btn)
        self._body_layout.addLayout(cfg_row)

        # bottom row
        row = QtWidgets.QHBoxLayout()
        lab = QtWidgets.QLabel("Theme")
        lab.setProperty("role", "muted")
        row.addWidget(lab)
        self.theme_combo = QtWidgets.QComboBox()
        for t in theming.available_themes():
            self.theme_combo.addItem(t.name, t.key)
        self._sync_combo(theme)
        self.theme_combo.currentIndexChanged.connect(self._combo_changed)
        row.addWidget(self.theme_combo)
        gallery_btn = QtWidgets.QPushButton("Theme gallery")
        gallery_btn.clicked.connect(self.open_gallery)
        row.addWidget(gallery_btn)
        row.addStretch(1)
        self._body_layout.addLayout(row)
        self.statusBar()

        self.setCentralWidget(central)
        self._apply_density(theme)

    def _apply_density(self, theme) -> None:
        pad, gap = theme.density.padding, theme.density.gap
        m = max(pad + 4, 12)
        self._body_layout.setContentsMargins(m, m, m, m)
        self._body_layout.setSpacing(gap)
        self._grid.setSpacing(gap)
        self._band_layout.setContentsMargins(m, max(4, pad - 2), m, max(4, pad - 2))
        lay = self.config_panel.layout()
        lay.setContentsMargins(pad, max(4, pad - 3), pad, max(4, pad - 3))
        lay.setHorizontalSpacing(gap + 6)
        lay.setVerticalSpacing(2)

    # ---------------------------------------------------------------- config panel
    def refresh_config(self) -> None:
        lay: QtWidgets.QGridLayout = self.config_panel.layout()
        keep = (self.catalog_value,)
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None and w not in keep:
                w.deleteLater()
        try:
            rows = corgidrp_config()
        except Exception as exc:
            msg = QtWidgets.QLabel(
                f"corgidrp could not be imported in this Python environment:\n"
                f"{type(exc).__name__}: {exc}\n\nActivate an environment where "
                f"'import corgidrp' works and restart corgidrp GUI.")
            msg.setProperty("role", "error")
            msg.setWordWrap(True)
            lay.addWidget(msg, 0, 0, 1, 2)
            self._add_catalog_row(1)
            return
        flags = [row for row in rows if row[0] in FLAG_KEYS]
        r = 0
        for key, value, tip in rows:
            if key in FLAG_KEYS:
                if key == FLAG_KEYS[0]:          # the three flags share one compact block
                    lay.addWidget(self._flags_block(flags), r, 0, 1, 2)
                    r += 1
                continue
            k = QtWidgets.QLabel(key)
            k.setProperty("role", "key")
            v = ElidedLabel(value)
            v.setProperty("role", "value")
            if tip:
                v.setToolTip(tip)
            lay.addWidget(k, r, 0)
            lay.addWidget(v, r, 1)
            r += 1
        self._add_catalog_row(r)
        lay.setColumnStretch(1, 1)

    @staticmethod
    def _flags_block(flags) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setVerticalSpacing(0)
        for c, (key, value, tip) in enumerate(flags):
            k = QtWidgets.QLabel(key)
            k.setProperty("role", "key")
            k.setToolTip(tip)
            v = QtWidgets.QLabel(value)
            v.setProperty("role", "value")
            g.addWidget(k, 0, c)
            g.addWidget(v, 1, c)
            g.setColumnStretch(c, 1)
        return w

    def _add_catalog_row(self, r: int) -> None:
        lay: QtWidgets.QGridLayout = self.config_panel.layout()
        k = QtWidgets.QLabel("step catalog")
        k.setProperty("role", "key")
        lay.addWidget(k, r, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.catalog_value, r, 1)

    # ---------------------------------------------------------------- step catalog
    def _show_catalog_status(self, st) -> None:
        text = st.one_line()
        self.catalog_value.setText(text)
        self.catalog_value.setToolTip(st.path)
        self.catalog_value.setProperty("chip", "warn" if st.stale or not st.exists else None)
        theming.repolish(self.catalog_value)

    def refresh_catalog_status(self) -> None:
        """Read the cache metadata only (instant; does not import corgidrp)."""
        try:
            st = _catalog_api().catalog_status(check_corgidrp=False)
        except Exception as exc:
            self.catalog_value.setText(f"unavailable: {type(exc).__name__}: {exc}")
            self.catalog_check.setEnabled(False)
            for a in self._rebuild_actions:
                a.setEnabled(False)
            if hasattr(self, "rebuild_button"):
                self.rebuild_button.setEnabled(False)
            return
        self._show_catalog_status(st)

    def _start(self, fn, on_done, on_failed) -> _Worker:
        worker = _Worker(fn, self)
        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        worker.finished.connect(lambda w=worker: (self._workers.discard(w), w.deleteLater()))
        self._workers.add(worker)
        worker.start()
        return worker

    def check_catalog(self) -> None:
        """Fingerprint the installed corgidrp in a thread and show whether the cache is stale."""
        self.catalog_check.setEnabled(False)
        self.catalog_check.setText("Checking\u2026")

        def done(st):
            self._show_catalog_status(st)
            self.catalog_check.setEnabled(True)
            self.catalog_check.setText("Check for changes")
            self.statusBar().showMessage(
                "Step catalog is out of date: " + "; ".join(st.stale_reasons) if st.stale
                else "Step catalog matches the installed corgidrp", 8000)

        def failed(msg):
            self.catalog_check.setEnabled(True)
            self.catalog_check.setText("Check for changes")
            self.statusBar().showMessage(f"Step catalog check failed: {msg}", 10000)

        api = _catalog_api()
        self._start(lambda: api.catalog_status(check_corgidrp=True), done, failed)

    def rebuild_catalog(self) -> None:
        """Rebuild the step catalog in a thread (imports corgidrp; a couple of seconds)."""
        if not self.rebuild_button.isEnabled():
            return
        self._set_rebuilding(True)
        self.statusBar().showMessage("Rebuilding step catalog\u2026")
        api = _catalog_api()

        def work():
            api.rebuild_catalog()
            return api.catalog_status(check_corgidrp=False)

        def done(st):
            self._set_rebuilding(False)
            self._show_catalog_status(st)
            self.statusBar().showMessage(
                f"Step catalog rebuilt: {st.n_steps} steps, {st.n_templates} templates", 10000)
            self.catalog_rebuilt.emit()

        def failed(msg):
            self._set_rebuilding(False)
            self.refresh_catalog_status()
            self.statusBar().showMessage(f"Step catalog rebuild failed: {msg}", 15000)

        self._start(work, done, failed)

    def _set_rebuilding(self, busy: bool) -> None:
        self.rebuild_button.setEnabled(not busy)
        self.rebuild_button.setText("Rebuilding\u2026" if busy else REBUILD_TEXT)
        self.catalog_check.setEnabled(not busy)
        for a in self._rebuild_actions:
            a.setEnabled(not busy)
        if busy:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
        else:
            QtWidgets.QApplication.restoreOverrideCursor()

    # ---------------------------------------------------------------- theme controls
    def _sync_combo(self, theme) -> None:
        i = self.theme_combo.findData(theme.key)
        if i >= 0 and i != self.theme_combo.currentIndex():
            blocked = self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(i)
            self.theme_combo.blockSignals(blocked)

    def _combo_changed(self, index: int) -> None:
        key = self.theme_combo.itemData(index)
        if key and key != theming.current_theme().key:
            theming.theme_manager().set_theme(key)

    def theme_changed(self, theme) -> None:
        self._sync_combo(theme)

    def redecorate(self, theme=None) -> None:
        theme = theme or theming.current_theme()
        super().redecorate(theme)
        self._apply_density(theme)       # after Panel's generic padding/spacing

    # ---------------------------------------------------------------- actions
    def set_tool_handler(self, key: str, handler) -> None:
        """Attach a real window factory for a tool; replaces the placeholder dialog."""
        self._handlers[key] = handler

    def _default_tool_handler(self, key: str) -> None:
        handler = self._handlers.get(key)
        if handler is not None:
            handler()
            return
        title, phase = next((t, p) for k, t, _s, p in TOOLS if k == key)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setText(f"{title} is not built yet.")
        box.setInformativeText(f"It arrives in phase {phase} of the corgidrp GUI plan.")
        box.open()

    def open_gallery(self) -> None:
        from .theme_gallery import ThemeGalleryWindow
        if self._gallery is None:
            self._gallery = ThemeGalleryWindow()
        self._gallery.show()
        self._gallery.raise_()
        self._gallery.activateWindow()

    def open_config_file(self) -> None:
        if not _backend().exists(str(CONFIG_FILE)):
            QtWidgets.QMessageBox.information(
                self, "Config file", f"{CONFIG_FILE} does not exist yet.\n"
                "corgidrp creates it the first time it is imported.")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(CONFIG_FILE)))

    def closeEvent(self, event):
        for w in list(self._workers):
            w.wait(10000)
        super().closeEvent(event)
        if event.isAccepted():
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.closeAllWindows()
                app.quit()
