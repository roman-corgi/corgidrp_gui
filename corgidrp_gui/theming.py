"""Theme engine: a theme is a whole style system, not a palette.

A theme is a TOML file (see ``corgidrp_gui/themes/*.toml``) with the sections
``meta``, ``palette``, ``type``, ``shape``, ``structure``, ``surface``,
``density`` and optional ``extras``. :func:`render_qss` turns a theme into a Qt
stylesheet through the Jinja2 template ``themes/base.qss.j2``;
:func:`apply_theme` installs fonts, palette and stylesheet on a QApplication.

Qt stylesheets cannot express letter-spacing, text case or drop shadows; those
parts of a theme are applied in Python (see :func:`label_text`,
:func:`label_font_tweaks` and :func:`decorate_panel`).
"""

from __future__ import annotations

import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import jinja2

PACKAGE_DIR = Path(__file__).resolve().parent
THEMES_DIR = PACKAGE_DIR / "themes"
FONTS_DIR = PACKAGE_DIR / "fonts"
#: the CorGI logo (launcher title band, window and dock icon)
LOGO_PATH = PACKAGE_DIR / "assets" / "corgi_logo.png"
#: the Roman Coronagraph logo (launcher title band, upper right)
ROMAN_LOGO_PATH = PACKAGE_DIR / "assets" / "roman_cgi_logo.png"
USER_THEMES_DIR = Path.home() / ".corgidrp_gui" / "themes"
DEFAULT_THEME = "roman"
SETTINGS_ORG = "corgidrp_gui"
SETTINGS_APP = "corgidrp_gui"
SETTINGS_KEY = "theme"

RAINBOW = ("#FF3B3B", "#FFB13B", "#FFF23B", "#3BFF6A", "#3BC8FF", "#7A3BFF", "#FF3BE4")

# Fallback stacks: if the first family is not installed, the first available one is used.
FONT_FALLBACKS: dict[str, list[str]] = {
    "Jost": ["Jost", "Avenir Next", "Helvetica Neue", "Segoe UI", "Arial"],
    "IBM Plex Mono": ["IBM Plex Mono", "Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"],
    "Share Tech Mono": ["Share Tech Mono", "Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"],
    "Silkscreen": ["Silkscreen", "Monaco", "Courier New"],
    "Helvetica": ["Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
    "Arial": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "Verdana": ["Verdana", "DejaVu Sans", "Tahoma", "Arial"],
    "Courier New": ["Courier New", "Courier", "Liberation Mono", "DejaVu Sans Mono"],
}
GENERIC_SANS = ["Helvetica Neue", "Segoe UI", "Arial", "DejaVu Sans"]
GENERIC_MONO = ["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"]


# --------------------------------------------------------------------------- model
@dataclass
class Meta:
    name: str = "Untitled"
    description: str = ""


@dataclass
class Palette:
    bg: str = "#ffffff"
    surface: str = "#ffffff"
    surface_alt: str = "#f4f4f4"
    text: str = "#000000"
    muted: str = "#555555"
    primary: str = "#00276b"
    action: str = "#00276b"
    secondary: str = "#4a6984"
    selection: str = "#cce4ff"
    selection_text: str = "#000000"
    ok: str = "#1f9d55"
    warn: str = "#d9a300"
    err: str = "#c00000"
    line: str = "#c0c0c0"
    title_bg: str = "#00276b"
    title_fg: str = "#ffffff"
    field: str = "#ffffff"


@dataclass
class TypeSpec:
    ui: Any = "Helvetica"          # str or list of family names
    heading: Any = "Helvetica"
    data: Any = "Courier New"
    size: int = 12
    label_case: str = "sentence"   # sentence | upper | plain
    label_tracking: float = 0.0    # em
    label_size: int = 0            # 0 -> size
    label_weight: int = 600
    label_color: str = "primary"   # a palette key


@dataclass
class Shape:
    radius: int = 4
    border_width: int = 1
    border_style: str = "line"     # line | bevel | none
    button: str = "flat"           # pill | flat | bevel | outline


@dataclass
class Structure:
    panel_frame: str = "line"      # none | line | bevel
    list_frame: str = "line"
    field_frame: str = "line"


@dataclass
class Surface:
    shadow: str = "none"           # none | soft
    gradient: bool = False
    glow: bool = False


@dataclass
class Density:
    padding: int = 8
    row_height: int = 22
    gap: int = 8


@dataclass
class Theme:
    key: str
    meta: Meta = field(default_factory=Meta)
    palette: Palette = field(default_factory=Palette)
    type: TypeSpec = field(default_factory=TypeSpec)
    shape: Shape = field(default_factory=Shape)
    structure: Structure = field(default_factory=Structure)
    surface: Surface = field(default_factory=Surface)
    density: Density = field(default_factory=Density)
    extras: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def name(self) -> str:
        return self.meta.name

    def extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)


_SECTIONS = {
    "meta": Meta, "palette": Palette, "type": TypeSpec, "shape": Shape,
    "structure": Structure, "surface": Surface, "density": Density,
}


def _build_section(cls, data: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys in [{cls.__name__}]: {sorted(unknown)}")
    return cls(**data)


def _theme_from_dict(key: str, data: dict, path: Path | None = None) -> Theme:
    kwargs = {name: _build_section(cls, data.get(name, {})) for name, cls in _SECTIONS.items()}
    theme = Theme(key=key, extras=dict(data.get("extras", {})), path=path, **kwargs)
    if theme.meta.name == "Untitled":
        theme.meta.name = key.capitalize()
    return theme


def _theme_dirs() -> list[Path]:
    dirs = [THEMES_DIR]
    if USER_THEMES_DIR.is_dir():
        dirs.append(USER_THEMES_DIR)
    return dirs


def load_theme(name_or_path: str | Path) -> Theme:
    """Load a theme by key (``"roman"``), display name (``"Roman"``) or TOML path."""
    path = Path(name_or_path)
    if path.suffix != ".toml" or not path.exists():
        key = str(name_or_path).lower()
        path = None
        for d in reversed(_theme_dirs()):          # user themes override package themes
            cand = d / f"{key}.toml"
            if cand.exists():
                path = cand
                break
        if path is None:
            for theme in available_themes():
                if theme.name.lower() == key:
                    return theme
            raise FileNotFoundError(f"no theme named {name_or_path!r}")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return _theme_from_dict(path.stem.lower(), data, path)


def available_themes() -> list[Theme]:
    """All themes: package themes, then ``~/.corgidrp_gui/themes/*.toml`` (overriding by key)."""
    found: dict[str, Theme] = {}
    for d in _theme_dirs():
        for p in sorted(d.glob("*.toml")):
            try:
                found[p.stem.lower()] = load_theme(p)
            except Exception:  # a broken user theme must not break the menu
                continue
    order = ["roman", "console", "neon", "homepage"]
    return sorted(found.values(), key=lambda t: (order.index(t.key) if t.key in order else 99, t.key))


# --------------------------------------------------------------------------- colors
def _rgb(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


def mix(a: str, b: str, t: float) -> str:
    """Blend ``a`` toward ``b`` by fraction ``t``."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex([x + (y - x) * t for x, y in zip(ra, rb)])


def lighten(c: str, t: float) -> str:
    return mix(c, "#ffffff", t)


def darken(c: str, t: float) -> str:
    return mix(c, "#000000", t)


def rgba(c: str, alpha: float) -> str:
    r, g, b = (round(v * 255) for v in _rgb(c))
    return f"rgba({r}, {g}, {b}, {round(alpha * 255)})"


def luminance(c: str) -> float:
    def lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def is_dark(c: str) -> bool:
    return luminance(c) < 0.2


def on(c: str, dark: str = "#141413", light: str = "#ffffff") -> str:
    """Readable text color on background ``c``."""
    contrast_light = (1.05) / (luminance(c) + 0.05)
    contrast_dark = (luminance(c) + 0.05) / (luminance(dark) + 0.05)
    return light if contrast_light >= contrast_dark else dark


# --------------------------------------------------------------------------- fonts
_fonts_registered = False


def register_fonts() -> list[str]:
    """Register bundled fonts once (needs a QGuiApplication). Returns the families added."""
    global _fonts_registered
    from qtpy import QtGui
    families: list[str] = []
    if _fonts_registered:
        return families
    for path in sorted(FONTS_DIR.glob("*.ttf")) + sorted(FONTS_DIR.glob("*.otf")):
        fid = QtGui.QFontDatabase.addApplicationFont(str(path.resolve()))
        if fid >= 0:
            families.extend(QtGui.QFontDatabase.applicationFontFamilies(fid))
    _fonts_registered = True
    return sorted(set(families))


def _font_stack(spec: Any, mono: bool) -> list[str]:
    names = [spec] if isinstance(spec, str) else list(spec)
    stack: list[str] = []
    for n in names:
        stack.extend(FONT_FALLBACKS.get(n, [n]))
    stack.extend(GENERIC_MONO if mono else GENERIC_SANS)
    seen: list[str] = []
    for n in stack:
        if n not in seen:
            seen.append(n)
    return seen


def resolve_family(spec: Any, mono: bool = False) -> str:
    """First installed family of the theme's font stack (first entry without a GUI app)."""
    stack = _font_stack(spec, mono)
    try:
        from qtpy import QtGui, QtWidgets
        if QtWidgets.QApplication.instance() is None:
            return stack[0]
        register_fonts()
        installed = set(QtGui.QFontDatabase.families())
    except Exception:
        return stack[0]
    for name in stack:
        if name in installed:
            return name
    return stack[0]


def theme_fonts(theme: Theme) -> dict[str, str]:
    return {
        "ui": resolve_family(theme.type.ui),
        "heading": resolve_family(theme.type.heading),
        "data": resolve_family(theme.type.data, mono=True),
    }


# --------------------------------------------------------------------------- icons
def _icon_dir(theme: Theme) -> Path:
    d = Path(tempfile.gettempdir()) / "corgidrp_gui_icons" / theme.key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, text: str) -> str:
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path.as_posix()


def ensure_icons(theme: Theme) -> dict[str, str]:
    """Small SVG glyphs colored for the theme (combo/spin arrows, check marks)."""
    p = theme.palette
    d = _icon_dir(theme)
    bevel = theme.shape.button == "bevel"
    fg = p.text if bevel else p.primary
    dis = mix(p.muted, p.bg, 0.5)
    chk = {"pill": on(p.action), "outline": p.primary}.get(theme.shape.button, p.text)

    def arrow(color, up):
        pts = "2,7 6,3 10,7" if up else "2,4 6,8 10,4"
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8" '
                f'stroke-linecap="square"/></svg>')

    def tri(color, up):
        pts = "1,8 6,3 11,8" if up else "1,4 6,9 11,4"
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                f'<polygon points="{pts}" fill="{color}"/></svg>')

    def check(color):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">'
                f'<polyline points="3,7.5 6,10.5 11.5,3.5" fill="none" stroke="{color}" '
                f'stroke-width="2.2" stroke-linecap="square"/></svg>')

    glyph = tri if bevel else arrow
    return {
        "down": _write(d / "down.svg", glyph(fg, False)),
        "up": _write(d / "up.svg", glyph(fg, True)),
        "down_disabled": _write(d / "down_dis.svg", glyph(dis, False)),
        "up_disabled": _write(d / "up_dis.svg", glyph(dis, True)),
        "check": _write(d / "check.svg", check(chk)),
        "check_disabled": _write(d / "check_dis.svg", check(dis)),
    }


# --------------------------------------------------------------------------- QSS
def derived_colors(theme: Theme) -> dict[str, str]:
    p = theme.palette
    dark = is_dark(p.bg)
    d = {
        "dark": dark,
        "hover": mix(p.surface, p.primary, 0.14 if dark else 0.07),
        "pressed": mix(p.surface, p.primary, 0.24 if dark else 0.15),
        "action_hover": mix(p.action, "#ffffff" if not dark else "#000000", 0.12)
        if theme.shape.button != "bevel" else lighten(p.action, 0.25),
        "action_pressed": darken(p.action, 0.15),
        "action_text": on(p.action, dark=p.primary if luminance(p.primary) < 0.05 else p.text),
        "disabled_text": mix(p.muted, p.bg, 0.45),
        "disabled_bg": mix(p.surface, p.bg, 0.5),
        "focus": p.secondary,
        "grid": mix(p.line, p.surface, 0.45),
        "label": getattr(p, theme.type.label_color, p.primary),
        "header_bg": p.surface_alt,
        "panel_top": lighten(p.surface, 0.05) if not dark else mix(p.surface, p.primary, 0.06),
        "button_top": mix(p.surface_alt, p.primary, 0.08) if dark else lighten(p.surface, 0.5),
        "handle": mix(p.line, p.muted, 0.35),
        "track": mix(p.bg, p.line, 0.25),
    }
    if theme.shape.border_style == "bevel" or theme.shape.button == "bevel":
        hi_base = p.surface if luminance(p.surface) > luminance(p.bg) else p.bg
        d["bevel_hi"] = lighten(hi_base, 0.85)
        d["bevel_lo"] = darken(p.line, 0.22 if luminance(p.line) < 0.3 else 0.36)
    else:
        d["bevel_hi"] = p.line
        d["bevel_lo"] = p.line
    for k in ("ok", "warn", "err", "run", "dim"):
        c = {"run": p.action, "dim": p.muted}.get(k) or getattr(p, k)
        if k == "run" and theme.key == "neon":
            c = p.primary
        d[f"chip_{k}"] = c
        d[f"chip_{k}_text"] = on(c)
        d[f"chip_{k}_tint"] = mix(p.surface, c, 0.14 if not dark else 0.18)
        d[f"chip_{k}_deep"] = darken(c, 0.25) if (not dark and luminance(c) > 0.25) else c
    return d


_env: jinja2.Environment | None = None


def _jinja() -> jinja2.Environment:
    global _env
    if _env is None:
        _env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(THEMES_DIR)),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        _env.filters.update(mix=mix, lighten=lighten, darken=darken, rgba=rgba, on=on)
    return _env


def render_qss(theme: Theme) -> str:
    """Render the Qt stylesheet for ``theme`` from ``themes/base.qss.j2``."""
    template = _jinja().get_template("base.qss.j2")
    ty = theme.type
    return template.render(
        t=theme, p=theme.palette, ty=ty, s=theme.shape, st=theme.structure,
        sf=theme.surface, dn=theme.density, x=theme.extras,
        d=derived_colors(theme), f=theme_fonts(theme), icons=ensure_icons(theme),
        label_size=ty.label_size or ty.size,
    )


# --------------------------------------------------------------------------- apply
_current: Theme | None = None


def current_theme() -> Theme:
    """The theme last applied (loads the default if none has been applied yet)."""
    global _current
    if _current is None:
        _current = load_theme(DEFAULT_THEME)
    return _current


def make_palette(theme: Theme):
    from qtpy import QtGui
    p = theme.palette
    d = derived_colors(theme)
    QC, R, G = QtGui.QColor, QtGui.QPalette.ColorRole, QtGui.QPalette.ColorGroup
    pal = QtGui.QPalette()
    roles = {
        R.Window: p.bg, R.WindowText: p.text, R.Base: p.field, R.AlternateBase: p.surface_alt,
        R.Text: p.text, R.Button: p.surface, R.ButtonText: p.text, R.BrightText: p.err,
        R.Highlight: p.selection, R.HighlightedText: p.selection_text,
        R.ToolTipBase: p.title_bg, R.ToolTipText: p.title_fg, R.Link: p.secondary,
        R.LinkVisited: p.primary, R.PlaceholderText: p.muted,
        R.Light: d["bevel_hi"], R.Midlight: mix(d["bevel_hi"], p.surface, 0.5),
        R.Mid: p.line, R.Dark: d["bevel_lo"], R.Shadow: darken(d["bevel_lo"], 0.4),
    }
    for role, color in roles.items():
        pal.setColor(role, QC(color))
    for role in (R.Text, R.WindowText, R.ButtonText):
        pal.setColor(G.Disabled, role, QC(d["disabled_text"]))
    pal.setColor(G.Disabled, R.Button, QC(d["disabled_bg"]))
    return pal


def apply_theme(app, theme: Theme) -> None:
    """Install fonts, application font, palette and stylesheet for ``theme``."""
    global _current
    from qtpy import QtGui, QtWidgets
    register_fonts()
    if app.style().objectName().lower() != "fusion":
        style = QtWidgets.QStyleFactory.create("Fusion")
        if style is not None:
            app.setStyle(style)
    fonts = theme_fonts(theme)
    font = QtGui.QFont(fonts["ui"])
    font.setPixelSize(theme.type.size)
    app.setFont(font)
    app.setPalette(make_palette(theme))
    _current = theme
    app.setStyleSheet(render_qss(theme))


# --------------------------------------------------------------------------- labels
def label_text(text: str, theme: Theme | None = None) -> str:
    """Apply the theme's label case to a section label text."""
    theme = theme or current_theme()
    case = theme.type.label_case
    if case == "upper":
        return text.upper()
    if case == "sentence" and text:
        return text[0].upper() + text[1:]
    return text


def label_font_tweaks(font, theme: Theme | None = None):
    """Return ``font`` with the theme's label tracking (letter-spacing is not QSS-able)."""
    from qtpy import QtGui
    theme = theme or current_theme()
    font = QtGui.QFont(font)
    tracking = theme.type.label_tracking
    size = theme.type.label_size or theme.type.size
    font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, tracking * size)
    return font


# --------------------------------------------------------------------------- decoration
_SHADOW_NAME = "corgidrp_gui_soft_shadow"
_GLOW_NAME = "corgidrp_gui_glow"


def decorate_panel(widget, theme: Theme | None = None) -> None:
    """Attach (or remove) the soft drop shadow for panel frames per the current theme."""
    from qtpy import QtGui, QtWidgets
    theme = theme or current_theme()
    effect = widget.graphicsEffect()
    ours = effect is not None and effect.objectName() == _SHADOW_NAME
    if theme.surface.shadow == "soft":
        if not ours:
            effect = QtWidgets.QGraphicsDropShadowEffect(widget)
            effect.setObjectName(_SHADOW_NAME)
            widget.setGraphicsEffect(effect)
        effect.setBlurRadius(12)
        effect.setOffset(0, 1)
        effect.setColor(QtGui.QColor(0, 39, 107, 40))
    elif ours:
        widget.setGraphicsEffect(None)


def decorate_glow(widget, color: str | None = None, theme: Theme | None = None) -> None:
    """Neon-style outer glow on a single widget (primary button, running step) when glow is on."""
    from qtpy import QtGui, QtWidgets
    theme = theme or current_theme()
    effect = widget.graphicsEffect()
    ours = effect is not None and effect.objectName() == _GLOW_NAME
    if theme.surface.glow:
        if not ours:
            effect = QtWidgets.QGraphicsDropShadowEffect(widget)
            effect.setObjectName(_GLOW_NAME)
            widget.setGraphicsEffect(effect)
        c = QtGui.QColor(color or theme.palette.action)
        c.setAlpha(170)
        effect.setBlurRadius(14)
        effect.setOffset(0, 0)
        effect.setColor(c)
    elif ours:
        widget.setGraphicsEffect(None)


def decorate_tree(root, theme: Theme | None = None) -> None:
    """Re-decorate a widget tree: panel shadows, glows, row heights, section labels."""
    from qtpy import QtWidgets
    theme = theme or current_theme()
    widgets = [root] + root.findChildren(QtWidgets.QWidget)
    for w in widgets:
        role = w.property("role")
        if role == "panel" and isinstance(w, QtWidgets.QFrame):
            decorate_panel(w, theme)
        elif w.property("glow"):
            decorate_glow(w, w.property("glow_color") or None, theme=theme)
        if isinstance(w, QtWidgets.QTableView):
            w.verticalHeader().setDefaultSectionSize(theme.density.row_height + 2)
        if hasattr(w, "apply_theme_text"):
            w.apply_theme_text(theme)


def repolish(widget) -> None:
    """Re-evaluate dynamic-property selectors after a property change."""
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    widget.update()


# --------------------------------------------------------------------------- manager
def _qt_core():
    from qtpy import QtCore
    return QtCore


def _make_manager_class():
    QtCore = _qt_core()

    class ThemeManager(QtCore.QObject):
        """Owns the active theme; emits ``changed(Theme)`` after a switch."""

        changed = QtCore.Signal(object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._theme: Theme | None = None

        @property
        def theme(self) -> Theme:
            return self._theme or current_theme()

        @staticmethod
        def settings():
            # honour QSettings.setDefaultFormat (tests redirect it to an ini file in a tmp dir)
            return QtCore.QSettings(QtCore.QSettings.defaultFormat(),
                                    QtCore.QSettings.Scope.UserScope, SETTINGS_ORG, SETTINGS_APP)

        def saved_theme_key(self) -> str:
            value = self.settings().value(SETTINGS_KEY, DEFAULT_THEME)
            return str(value) if value else DEFAULT_THEME

        def set_theme(self, name_or_theme, persist: bool = True) -> Theme:
            from qtpy import QtWidgets
            theme = name_or_theme if isinstance(name_or_theme, Theme) else load_theme(name_or_theme)
            app = QtWidgets.QApplication.instance()
            if app is not None:
                apply_theme(app, theme)
            else:
                global _current
                _current = theme
            self._theme = theme
            if persist:
                self.settings().setValue(SETTINGS_KEY, theme.key)
            self.changed.emit(theme)
            return theme

        def restore(self) -> Theme:
            try:
                return self.set_theme(self.saved_theme_key(), persist=False)
            except FileNotFoundError:
                return self.set_theme(DEFAULT_THEME, persist=False)

    return ThemeManager


_manager = None
_manager_cls = None


def theme_manager():
    """The process-wide ThemeManager."""
    global _manager, _manager_cls
    if _manager is None:
        if _manager_cls is None:
            _manager_cls = _make_manager_class()
        _manager = _manager_cls()
    return _manager


def __getattr__(name):  # expose ThemeManager class lazily (keeps this module importable without Qt)
    global _manager_cls
    if name == "ThemeManager":
        if _manager_cls is None:
            _manager_cls = _make_manager_class()
        return _manager_cls
    raise AttributeError(name)
