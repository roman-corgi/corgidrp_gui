# Changelog

## Unreleased

- Recipe Builder: *Save & queue* fills in AUTOMATIC calibrations from the CalDB first, for each
  split part, as corgidrp's walker does for recipes it generates. Before, they were queued as
  AUTOMATIC and the step failed opening a file named "AUTOMATIC" (unless jit_calib_id was on).
  When the CalDB lacks a required calibration, queueing asks first, as the Parser does.
- Status Console: recipe files queued from the Queue menu, by drag and drop or with `--queue`
  get the same CalDB lookup and missing-calibration question.
- Midnight theme: a calm dark theme without glow or neon.
- CalDB viewer: Remove missing entries button.
- Status Console: unticking "Show only the selected job" restores the combined log.
- Recipe Builder: double-click an argument's description to read it in full.
- Recipe Builder: Start from template… button and dialog (searchable, with a step preview).
- Parser and Builder: split inputs into several recipes by a maximum number of files per
  recipe and/or by header keywords (VISITID, DPAMNAME, ...); all parts queue together.
- Add folder… (Parser and Builder inputs) accepts several folders at once, with an
  include-subfolders switch and a filename pattern.
- Parser: a missing required calibration no longer means no recipe. The group is marked
  *incomplete*, the recipe is made with that calibration left AUTOMATIC (and jit_calib_id on),
  the Builder flags each gap, and queueing asks before including it.
- Dataset Summary window: groups any set of files by the keywords corgidrp branches and
  splits on (presets or your own), folds levels where files agree, shows per-group keyword
  distributions and what varies, and hands groups to the Parser. `--summarize PATH`.
- Header reading is about 25x faster on reduced data: the long RECIPE/RECIPE2/... JSON cards
  keep only the recipe name.
- docs/remote_xpra.md rewritten from a real setup: focal repository recipe, Xpra-Light
  client, start-child by absolute path (no wrapper script), troubleshooting section.

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
