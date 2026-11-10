"""Theme engine, stylesheet and themed windows (offscreen Qt)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from corgidrp_gui import theming

THEME_KEYS = ["roman", "console", "neon", "homepage", "midnight"]
BAD_QT_MESSAGES = ("Could not parse", "Unknown property")
settings_dir = None


@pytest.fixture(scope="module")
def qt_messages():
    from qtpy import QtCore
    messages: list[str] = []

    def handler(mode, context, message):
        messages.append(message)

    previous = QtCore.qInstallMessageHandler(handler)
    yield messages
    QtCore.qInstallMessageHandler(previous)


@pytest.fixture(scope="module")
def app(qt_messages, tmp_path_factory):
    from qtpy import QtCore
    # keep QSettings writes (theme choice) out of the user's real settings
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    global settings_dir
    settings_dir = tmp_path_factory.mktemp("qsettings")
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
                             str(settings_dir))
    from corgidrp_gui.app import make_app
    return make_app([], restore=False)


def _bad(messages):
    return [m for m in messages if any(b in m for b in BAD_QT_MESSAGES)]


def test_package_themes_present():
    keys = [t.key for t in theming.available_themes()]
    for k in THEME_KEYS:
        assert k in keys


@pytest.mark.parametrize("key", THEME_KEYS)
def test_theme_loads(key):
    t = theming.load_theme(key)
    assert t.key == key
    assert t.meta.name and t.meta.description
    for name in ("bg", "surface", "text", "primary", "action", "selection", "line", "field"):
        assert getattr(t.palette, name).startswith("#")
    assert t.type.label_case in ("sentence", "upper", "plain")
    assert t.shape.border_style in ("line", "bevel", "none")
    assert t.shape.button in ("pill", "flat", "bevel", "outline")
    for axis in ("panel_frame", "list_frame", "field_frame"):
        assert getattr(t.structure, axis) in ("none", "line", "bevel")
    assert t.surface.shadow in ("none", "soft")
    assert t.density.row_height > 0
    assert theming.load_theme(t.meta.name).key == key       # by display name
    assert theming.load_theme(t.path).key == key            # by path


def test_theme_specifics():
    roman, console, neon, home = (theming.load_theme(k) for k in THEME_KEYS[:4])
    assert roman.palette.action == "#e2433b" and roman.surface.shadow == "soft"
    assert console.shape.button == "bevel" and console.structure.panel_frame == "bevel"
    assert neon.surface.glow and neon.surface.gradient and neon.type.label_case == "upper"
    assert home.extra("rainbow_rule") is True and roman.extra("rainbow_rule") is None


@pytest.mark.parametrize("key", THEME_KEYS)
def test_render_qss_clean(key):
    qss = theming.render_qss(theming.load_theme(key))
    for token in ("{{", "}}", "{%", "%}", "None"):
        assert token not in qss, token
    assert qss.count("{") == qss.count("}")
    for selector in ('QPushButton[role="primary"]', 'QLabel[role="section"]',
                     'QFrame[role="panel"]', 'QLabel[chip="warn"]', 'QTextEdit[role="log"]',
                     "QProgressBar::chunk", "QHeaderView::section", "QScrollBar", "QToolTip",
                     "QSplitter::handle", "QStatusBar", "QTabBar::tab", "QComboBox::drop-down"):
        assert selector in qss, selector


def test_structural_axes_reach_qss():
    console = theming.render_qss(theming.load_theme("console"))
    assert "border-style: outset" in console and "border-style: inset" in console
    roman = theming.render_qss(theming.load_theme("roman"))
    assert "outset" not in roman
    neon = theming.render_qss(theming.load_theme("neon"))
    assert "qlineargradient" in neon
    home = theming.render_qss(theming.load_theme("homepage"))
    assert "border-width: 3px" in home


@pytest.mark.parametrize("key", THEME_KEYS)
def test_apply_theme_no_qss_warnings(app, qt_messages, key):
    from qtpy import QtWidgets
    theme = theming.load_theme(key)
    qt_messages.clear()
    theming.apply_theme(app, theme)
    assert theming.current_theme().key == key
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    for cls in (QtWidgets.QPushButton, QtWidgets.QLineEdit, QtWidgets.QComboBox,
                QtWidgets.QSpinBox, QtWidgets.QCheckBox, QtWidgets.QTableView,
                QtWidgets.QListView, QtWidgets.QTreeView, QtWidgets.QProgressBar,
                QtWidgets.QTabWidget, QtWidgets.QTextEdit, QtWidgets.QGroupBox):
        lay.addWidget(cls())
    w.show()
    app.processEvents()
    w.grab()
    w.close()
    assert not _bad(qt_messages), _bad(qt_messages)
    assert app.palette().color(app.palette().ColorRole.Window).name().lower() == \
        theme.palette.bg.lower()


def test_fonts_registered(app):
    from qtpy import QtGui
    families = set(QtGui.QFontDatabase.families())
    bundled = {p.stem.split("-")[0] for p in theming.FONTS_DIR.glob("*.ttf")}
    expected = {"Jost": "Jost", "Silkscreen": "Silkscreen", "ShareTechMono": "Share Tech Mono",
                "IBMPlexMono": "IBM Plex Mono"}
    for stem, family in expected.items():
        if stem in bundled:
            assert family in families, family


def test_decorate_panel_follows_theme(app):
    from qtpy import QtWidgets
    frame = QtWidgets.QFrame()
    theming.apply_theme(app, theming.load_theme("roman"))
    theming.decorate_panel(frame)
    assert isinstance(frame.graphicsEffect(), QtWidgets.QGraphicsDropShadowEffect)
    theming.decorate_panel(frame)        # idempotent
    assert frame.graphicsEffect() is not None
    theming.apply_theme(app, theming.load_theme("console"))
    theming.decorate_panel(frame)
    assert frame.graphicsEffect() is None


def test_label_text_case():
    assert theming.label_text("step catalog", theming.load_theme("neon")) == "STEP CATALOG"
    assert theming.label_text("step catalog", theming.load_theme("roman")) == "Step catalog"
    assert theming.label_text("step catalog", theming.load_theme("console")) == "step catalog"


def test_theme_manager_persists(app):
    from qtpy import QtCore
    mgr = theming.theme_manager()
    seen = []
    mgr.changed.connect(seen.append)
    try:
        mgr.set_theme("neon")
        assert QtCore.QSettings.defaultFormat() == QtCore.QSettings.Format.IniFormat
        assert mgr.settings().fileName().startswith(str(settings_dir))
        assert mgr.settings().value("theme") == "neon"
        assert seen and seen[-1].key == "neon"
        mgr.set_theme("roman", persist=False)
        assert mgr.saved_theme_key() == "neon"
    finally:
        mgr.changed.disconnect(seen.append)


@pytest.mark.parametrize("key", THEME_KEYS)
def test_windows_construct_and_show(app, qt_messages, key):
    from corgidrp_gui.ui.launcher import LauncherWindow
    from corgidrp_gui.ui.theme_gallery import ThemeGalleryWindow
    theming.theme_manager().set_theme(key, persist=False)
    qt_messages.clear()
    for cls in (LauncherWindow, ThemeGalleryWindow):
        win = cls()
        win.show()
        app.processEvents()
        assert not win.grab().isNull()
        assert win.rainbow_rule.isVisibleTo(win) == (key == "homepage")
        theme_actions = [a for a in win.menuBar().actions() if a.text().replace("&", "") == "Theme"]
        assert theme_actions
        win.hide()
        win.deleteLater()
    app.processEvents()
    assert not _bad(qt_messages), _bad(qt_messages)


def test_launcher_live_switch_and_signals(app):
    from corgidrp_gui.ui.launcher import TOOLS, LauncherWindow
    theming.theme_manager().set_theme("roman", persist=False)
    win = LauncherWindow()
    win.show()
    app.processEvents()
    assert win.tiles["parser"].property("role") == "primary"
    assert win.windowTitle() == "corgidrp GUI"
    requested = []
    win.tool_requested.connect(requested.append)
    win.set_tool_handler("parser", lambda: requested.append("handled"))
    win.tiles["parser"].click()
    assert sorted(requested) == ["handled", "parser"]
    assert len(TOOLS) == 5
    # switching theme from outside updates the combo and the rainbow rule
    theming.theme_manager().set_theme("homepage", persist=False)
    app.processEvents()
    assert win.theme_combo.currentData() == "homepage"
    assert win.rainbow_rule.isVisibleTo(win)
    assert win.config_label.text() == "CONFIGURATION"
    theming.theme_manager().set_theme("roman", persist=False)
    assert win.config_label.text() == "Configuration"
    assert not win.rainbow_rule.isVisibleTo(win)
    win.hide()
    win.deleteLater()


def test_launcher_config_rows():
    from corgidrp_gui.ui.launcher import corgidrp_config
    rows = dict((k, v) for k, v, _ in corgidrp_config())
    import corgidrp
    assert rows["corgidrp"].startswith(corgidrp.__version__)
    for k in ("caldb", "user templates", "jit_calib_id", "skip_missing_cal_steps",
              "enforce_template_structure", "walker"):
        assert k in rows
    assert "steps" in rows["walker"] and "templates" in rows["walker"]


def _wait(app, predicate, timeout_ms=60000):
    from qtpy import QtCore
    timer = QtCore.QElapsedTimer()
    timer.start()
    while not predicate() and timer.elapsed() < timeout_ms:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
    return predicate()


def test_launcher_step_catalog_rebuild(app, tmp_path, monkeypatch):
    """Status line at startup is read from the cache only; rebuild runs in a thread."""
    monkeypatch.setenv("CORGIDRP_GUI_HOME", str(tmp_path))
    from corgidrp_gui.ui.launcher import LauncherWindow
    theming.theme_manager().set_theme("roman", persist=False)
    win = LauncherWindow()
    win.show()
    app.processEvents()
    assert not (tmp_path / "step_catalog.json").exists()        # never built automatically
    assert "no step catalog" in win.catalog_value.text()
    assert win.catalog_value.property("chip") == "warn"
    rebuilt = []
    win.catalog_rebuilt.connect(lambda: rebuilt.append(True))
    win.rebuild_catalog()
    assert not win.rebuild_button.isEnabled()                    # busy state
    assert _wait(app, lambda: rebuilt and not win._workers)
    assert (tmp_path / "step_catalog.json").exists()
    assert win.rebuild_button.isEnabled()
    assert win.statusBar().currentMessage().startswith("Step catalog rebuilt:")
    assert "steps" in win.catalog_value.text()
    win.check_catalog()
    assert _wait(app, lambda: not win._workers and win.catalog_check.isEnabled())
    assert win.catalog_value.property("chip") in (None, "warn")
    win.hide()
    win.deleteLater()
