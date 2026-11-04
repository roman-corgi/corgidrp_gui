# Changelog

## Unreleased

- Midnight theme: a calm dark theme without glow or neon.
- CalDB viewer: Remove missing entries button.
- Status Console: unticking "Show only the selected job" restores the combined log.
- Recipe Builder: double-click an argument's description to read it in full.
- Recipe Builder: Start from template… button and dialog (searchable, with a step preview).
- Parser and Builder: split inputs into several recipes by a maximum number of files per
  recipe and/or by header keywords (VISITID, DPAMNAME, ...); all parts queue together.

## 0.1.0 — 2026-10-20

First complete version.

- Launcher with live corgidrp configuration and an explicit step-catalog rebuild.
- Recipe Parser: group files the way the walker branches, recommend templates with an
  explanation, populate recipes via autogen_recipe, override, preview, queue.
- Recipe Builder: step catalog with template-informed placement, drag-and-drop steps,
  inspector with arguments/defaults/docstrings, calibration slots with a CalDB chooser,
  pop-out editor for complex values, live validation, save as recipe or user template.
- Status Console: persistent queue with chain dependencies, one runner process per recipe,
  progress from the walker's console output, pause/abort/re-queue, log and traceback.
- Calibration DB viewer: browse/filter, scan, add, remove, and "which file would be chosen".
- Four style themes (Roman, Console, Neon, Homepage '98) and custom themes from TOML.
- Command line: --theme, --tool, --files, --recipe, --queue; `corgidrp_gui.runner` for
  running recipes without the GUI.
