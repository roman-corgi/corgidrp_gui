"""Recipe Parser core: from a pile of FITS files to recommended recipes.

The recommendation itself comes from corgidrp (:func:`corgidrp.walker.guess_template`
and :func:`corgidrp.walker.autogen_recipe`). This module groups files the way the
walker will see them, calls those functions per group, and explains the choice
in terms of the header keywords the walker actually looks at.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..backend import Backend, FitsHeaders, default_backend
from .recipes import Recipe
from .splitting import Batch, SplitSpec, batch_recipe_name, split_records

__all__ = [
    "DISPLAY_KEYS", "GROUP_KEYS", "FileRecord", "Facts", "Recommendation", "FileGroup",
    "files_in_folders", "scan_files", "group_files", "split_group", "recommend", "build_recipes",
    "build_incomplete_recipes", "normalize_chains",
    "GroupAnalysis", "analyze_group", "analyze",
]

#: header keywords worth a column in the file table (ext header first, then primary)
DISPLAY_KEYS = ["DATALVL", "VISTYPE", "DPAMNAME", "CFAMNAME", "FPAMNAME", "LSAMNAME", "ISPC",
                "EXPTIME", "EMGAIN_C", "KGAINPAR", "FSMX", "FSMY", "TARGET", "VISITID", "DATETIME", "ARRTYPE"]
#: keywords that define a group; mirrors the top-level branching of guess_template
GROUP_KEYS = ["DATALVL", "VISTYPE", "DPAMNAME", "ISPC"]
#: the (EXPTIME, EMGAIN_C, KGAINPAR) split the walker does for darks
SETTING_KEYS = ["EXPTIME", "EMGAIN_C", "KGAINPAR"]
_LEVEL_RANK = {"L1": 0, "L2a": 1, "L2b": 2, "L3": 3, "L4": 4}


@dataclass
class FileRecord:
    path: str
    headers: FitsHeaders

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ok(self) -> bool:
        return self.headers.error is None

    def value(self, key: str, default=None):
        return self.headers.get(key, default)

    def row(self, keys=DISPLAY_KEYS) -> dict[str, Any]:
        return {"File": self.name, **{k: self.value(k, "") for k in keys}}


@dataclass
class Facts:
    """What guess_template looks at, computed from headers only."""
    n_files: int = 0
    datalvl: Any = None
    vistype: Any = None
    dpamname: Any = None
    ispc_values: list = field(default_factory=list)
    n_settings: int = 0                    # unique (EXPTIME, EMGAIN_C, KGAINPAR)
    fsm_differ: bool | None = None
    has_nd_filter: bool = False
    is_prism: bool = False
    is_pol: bool = False
    vistype_values: list = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"DATALVL = {self.datalvl}", f"VISTYPE = {self.vistype if self.vistype is not None else '(absent)'}",
               f"DPAMNAME = {self.dpamname}"]
        if self.ispc_values:
            out.append("ISPC = " + ", ".join(str(v) for v in self.ispc_values) + (" (mixed)" if len(self.ispc_values) > 1 else ""))
        out.append(f"{self.n_settings} unique (EXPTIME, EMGAIN_C, KGAINPAR) setting{'s' if self.n_settings != 1 else ''}")
        if self.fsm_differ is not None:
            out.append("FSM positions differ" if self.fsm_differ else "FSM positions identical")
        if self.has_nd_filter:
            out.append("ND filter present (FPAMNAME)")
        if self.is_prism:
            out.append("spectroscopy (PRISM in DPAMNAME)")
        if self.is_pol:
            out.append("polarimetry (POL0/POL45 in DPAMNAME)")
        return out


@dataclass
class Recommendation:
    chains: list[list[str]]                # list of chains; each chain is template filenames run in sequence
    chained: bool
    facts: Facts
    reason: str = ""
    error: str | None = None
    log: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.chains)

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error.splitlines()[-1]}"
        parts = []
        for chain in self.chains:
            parts.append(" → ".join(c.replace(".json", "") for c in chain))
        return " | ".join(parts)


@dataclass
class FileGroup:
    key: tuple
    files: list[FileRecord]
    recommendation: Recommendation | None = None
    batch: Batch | None = None                 #: set when the group was split by the user's rules

    @property
    def label(self) -> str:
        datalvl, vistype, dpam, ispc = self.key
        bits = [str(datalvl), _short_vistype(vistype)]
        if dpam not in (None, "", "IMAGING"):
            bits.append(str(dpam))
        if ispc == 1:
            bits.append("PC")
        if self.batch is not None and self.batch.label:
            bits.append(self.batch.label)
        return " · ".join(bits)

    @property
    def batch_slug(self) -> str:
        return self.batch.slug if self.batch is not None else ""

    @property
    def override_key(self) -> tuple:
        """Identifies the group *and* its batch; what manual template overrides are keyed by."""
        return (*self.key, self.batch_slug)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def _short_vistype(v) -> str:
    if v is None:
        return "no VISTYPE"
    return re.sub(r"^CGIVST_", "", str(v))


# --------------------------------------------------------------------------- #
# scanning and grouping
# --------------------------------------------------------------------------- #
def files_in_folders(folders: list[str], pattern: str = "*.fits", recursive: bool = False,
                     backend: Backend | None = None) -> list[str]:
    """Every file matching ``pattern`` in the given folders (optionally their subfolders),
    de-duplicated and sorted; folders are visited in the order given."""
    backend = backend or default_backend()
    seen: set[str] = set()
    out: list[str] = []
    for d in folders:
        for f in backend.glob_fits(d, pattern or "*.fits", recursive=recursive):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def scan_files(paths: list[str], backend: Backend | None = None, progress=None) -> list[FileRecord]:
    """Read headers (only) of every path. ``progress(i, n)`` is called per file."""
    backend = backend or default_backend()
    out = []
    n = len(paths)
    for i, p in enumerate(paths):
        out.append(FileRecord(p, backend.read_headers(p)))
        if progress:
            progress(i + 1, n)
    return out


def group_files(records: list[FileRecord]) -> list[FileGroup]:
    """Group by DATALVL, VISTYPE, DPAMNAME, ISPC; calibrations first, then by level."""
    groups: dict[tuple, list[FileRecord]] = {}
    for r in records:
        if not r.ok:
            key = ("unreadable", None, None, None)
        else:
            key = tuple(r.value(k) for k in GROUP_KEYS)
        groups.setdefault(key, []).append(r)

    def sort_key(item):
        (lvl, vistype, dpam, ispc), _ = item
        is_cal = 0 if (vistype and "CAL" in str(vistype)) else 1
        return (lvl == "unreadable", is_cal, _LEVEL_RANK.get(str(lvl), 9), str(vistype), str(dpam), str(ispc))

    return [FileGroup(k, v) for k, v in sorted(groups.items(), key=sort_key)]


def split_group(group: FileGroup, spec: SplitSpec | None) -> list[FileGroup]:
    """Apply the user's splitting rules to one group: one FileGroup per batch."""
    if spec is None or not spec.active or group.key[0] == "unreadable":
        return [group]
    return [FileGroup(group.key, b.items, batch=b) for b in split_records(group.files, spec)]


def compute_facts(records: list[FileRecord]) -> Facts:
    f = Facts(n_files=len(records))
    if not records:
        return f
    first = records[0]
    f.datalvl = first.value("DATALVL")
    f.vistype = first.value("VISTYPE")
    f.dpamname = first.value("DPAMNAME", "")
    f.ispc_values = sorted({r.value("ISPC") for r in records if r.value("ISPC") is not None}, key=str)
    f.n_settings = len({tuple(r.value(k) for k in SETTING_KEYS) for r in records})
    f.vistype_values = sorted({str(r.value("VISTYPE")) for r in records})
    fsm = {(r.value("FSMX"), r.value("FSMY")) for r in records}
    if any(x != (None, None) for x in fsm):
        xs = [x for x, _ in fsm if x is not None]
        ys = [y for _, y in fsm if y is not None]
        f.fsm_differ = (max(xs) - min(xs) > 4.0) if xs else False or ((max(ys) - min(ys) > 4.0) if ys else False)
    f.has_nd_filter = any(str(r.value("FPAMNAME", "")).startswith("ND") for r in records)
    dp = str(f.dpamname or "")
    f.is_prism = dp.startswith("PRISM")
    f.is_pol = dp in ("POL0", "POL45")
    return f


# --------------------------------------------------------------------------- #
# recommendation
# --------------------------------------------------------------------------- #
def normalize_chains(recipe_filename, chained: bool) -> tuple[list[list[str]], bool]:
    """guess_template returns str | list[str] | list[list[str]]; make it list[list[str]]."""
    if isinstance(recipe_filename, str):
        return [[recipe_filename]], False
    if recipe_filename and isinstance(recipe_filename[0], list):
        return [list(c) for c in recipe_filename], True
    if chained:
        return [list(recipe_filename)], True
    # several independent recipes on the same input
    return [[r] for r in recipe_filename], False


def recommend(group: FileGroup) -> Recommendation:
    """Ask corgidrp which template(s) fit this group, and say why."""
    facts = compute_facts(group.files)
    log = io.StringIO()
    if group.key[0] == "unreadable":
        return Recommendation([], False, facts, error="files could not be read as FITS")
    try:
        import corgidrp.data as cdata
        import corgidrp.walker as walker
        with contextlib.redirect_stdout(log):
            ds = cdata.Dataset(group.paths, no_data=True, no_err=True, no_dq=True)
            recipe_filename, chained = walker.guess_template(ds)
        chains, chained = normalize_chains(recipe_filename, chained)
    except Exception:  # noqa: BLE001 - surface anything corgidrp raises
        return Recommendation([], False, facts, error=traceback.format_exc(), log=log.getvalue())
    rec = Recommendation(chains, chained, facts, log=log.getvalue())
    rec.reason = _explain(facts, rec)
    group.recommendation = rec
    return rec


def _explain(f: Facts, rec: Recommendation) -> str:
    """Plain-language account of the branch guess_template took."""
    lvl, vt = str(f.datalvl), f.vistype
    first = rec.chains[0][0] if rec.chains else ""
    why: list[str] = []
    if lvl == "L1":
        if vt is None:
            why.append("no VISTYPE keyword, so this is treated as IIT test data and gets generic L1→L2b processing")
        elif str(vt).startswith("CGIVST_CAL_"):
            why.append(f"VISTYPE {vt} is a calibration visit")
            if vt == "CGIVST_CAL_DRK":
                if 1 in f.ispc_values:
                    why.append("ISPC = 1, so photon-counted darks")
                elif f.n_settings > 1:
                    why.append(f"{f.n_settings} exposure/gain settings, so darks for noise-map creation")
                else:
                    why.append("one exposure/gain setting, so a traditional master dark")
            elif vt in ("CGIVST_CAL_ABSFLUX_FAINT", "CGIVST_CAL_ABSFLUX_BRIGHT"):
                if f.is_prism:
                    why.append("ND filter present, so ND-filter spectroscopy calibration" if f.has_nd_filter else "no ND filter, so spectroscopic flux calibration")
                elif f.is_pol:
                    why.append("polarizer in beam, so polarimetric flux calibration")
                elif len(f.vistype_values) > 1 or vt == "CGIVST_CAL_ABSFLUX_BRIGHT":
                    why.append("bright targets present, so ND filter calibration")
                else:
                    why.append("faint targets only, so absolute flux calibration")
            elif vt == "CGIVST_CAL_POL_SETUP":
                why.append("FSM positions differ, so a polarimetric calibration" if f.fsm_differ else "FSM positions identical, so straight to L3 polarimetry")
            elif vt == "CGIVST_CAL_FLAT":
                why.append("polarizer in beam, so a polarimetric flat" if f.is_pol else "flat and bad-pixel-map recipe")
        else:
            why.append(f"VISTYPE {vt} is not a calibration visit type, so science processing L1→L2a")
    elif lvl == "L2a":
        if vt == "CGIVST_CAL_DRK":
            why.append("L2a darks" + (": photon counted" if 1 in f.ispc_values else (": multiple settings, noise maps" if f.n_settings > 1 else ": traditional master dark")))
        else:
            mode = "spectroscopy" if f.is_prism else ("polarimetry" if f.is_pol else "imaging")
            if len(f.ispc_values) > 1:
                why.append(f"mixed analog and photon-counted frames in {mode}; two chains, one per kind")
            elif 1 in f.ispc_values:
                why.append(f"photon-counted {mode}")
            else:
                why.append(f"analog {mode}")
    elif lvl == "L2b":
        if vt in ("CGIVST_CAL_ABSFLUX_FAINT", "CGIVST_CAL_ABSFLUX_BRIGHT"):
            why.append("absolute flux visit at L2b")
        elif vt == "CGIVST_CAL_POL_SETUP":
            why.append("FSM positions differ, so polcal" if f.fsm_differ else "to L3 polarimetry")
        elif f.is_pol:
            why.append("polarimetry to L3")
        else:
            why.append("L2b → L3" + ("" if vt and ("TDD" in str(vt) or vt == "CGIVST_CAL_SPEC_TGTREF") else "; note the walker warns that only TDD and some cal frames should go beyond L2b"))
    elif lvl == "L3":
        if f.is_pol:
            why.append("polarimetry L3 → L4")
        elif f.is_prism:
            why.append("spectroscopy L3 → L4" + (" without PSF subtraction" if vt == "CGIVST_CAL_SPEC_TGTREF" else " with PSF subtraction"))
        else:
            why.append("imaging L3 → L4" + (" without PSF subtraction" if vt == "CGIVST_CAL_TGTREF_PHOT" else " with PSF subtraction"))
    tail = f" → {first.replace('.json', '')}" if first else ""
    if rec.chained and rec.chains and len(rec.chains[0]) > 1:
        tail += f", chained through {len(rec.chains[0])} recipes"
    if len(rec.chains) > 1:
        tail += f"; {len(rec.chains)} separate chains"
    return "; ".join(why) + tail


# --------------------------------------------------------------------------- #
# recipe generation
# --------------------------------------------------------------------------- #
def build_recipes(paths: list[str], outputdir: str, template=None) -> tuple[list[list[Recipe]], str]:
    """Fully populated recipes via corgidrp's autogen_recipe.

    ``template`` may be None (let corgidrp guess), a template filename, a path,
    or a recipe dict. Returns ``(chains, log)`` where ``chains`` is a list of
    chains and each chain a list of :class:`Recipe`. Raises whatever corgidrp
    raises (typically a missing calibration in the CalDB).
    """
    import corgidrp.walker as walker
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        if isinstance(template, str) and os.path.sep not in template:
            tpl, _path, _user = walker._load_recipe_template(template)
        elif isinstance(template, str):
            import json
            with open(template) as f:
                tpl = json.load(f)
        else:
            tpl = template
        result = walker.autogen_recipe(list(paths), outputdir, template=tpl)
    # normalise: recipe | list[recipe] | list[list[recipe]]
    if isinstance(result, dict):
        chains = [[result]]
    elif result and isinstance(result[0], dict):
        chains = [list(result)]
    else:
        chains = [list(c) for c in result]
    return [[Recipe.from_dict(r) for r in chain] for chain in chains], log.getvalue()


def _load_template(template) -> tuple[dict, str | None]:
    """A private copy of a template given by name, path or dict, plus where it came from.
    Template names are looked up the way the walker does (user_templates first)."""
    if isinstance(template, dict):
        return copy.deepcopy(template), None
    if os.path.sep not in template:
        import corgidrp
        import corgidrp.walker as walker
        tpl, path, is_user = walker._load_recipe_template(template)
        if is_user and corgidrp.enforce_template_structure:
            walker._validate_template_structure(tpl, os.path.join(walker.recipe_dir, template), template)
        return copy.deepcopy(tpl), path
    with open(template) as f:
        return json.load(f), template


def build_incomplete_recipes(paths: list[str], outputdir: str, chains: list[list], chained: bool
                             ) -> tuple[list[list[Recipe]], str]:
    """Recipes for ``paths`` even when the CalDB lacks a calibration they need.

    Used after :func:`build_recipes` failed on a missing calibration. Each template gets the
    recipe-level ``jit_calib_id`` flag (a documented corgidrp recipe setting), so
    ``autogen_recipe`` fills inputs and output directory without looking calibrations up.
    Calibrations are then looked up here, one by one, exactly as the walker would:
    found ones get their file path, missing ones stay "AUTOMATIC" and are listed in
    :attr:`Recipe.unresolved`. A recipe with a missing required calibration keeps
    ``jit_calib_id`` on, so the walker looks it up again when the step runs; a recipe
    without one gets its original setting back (optional misses become None, as usual).

    ``chains`` holds template names, paths or dicts, one list per chain.
    """
    import corgidrp.caldb as caldb
    import corgidrp.data as cdata
    import corgidrp.walker as walker
    log = io.StringIO()
    out: list[list[Recipe]] = []
    with contextlib.redirect_stdout(log):
        ref = cdata.Dataset([paths[0]])[0] if paths else None
        db = caldb.CalDB()
        for chain in chains:
            built = []
            for i, template in enumerate(chain):
                tpl, src = _load_template(template)
                cfg = tpl.get("drpconfig")
                if not isinstance(cfg, dict):
                    cfg = tpl["drpconfig"] = {}
                had_jit, old_jit = "jit_calib_id" in cfg, cfg.get("jit_calib_id")
                cfg["jit_calib_id"] = True
                r = walker.autogen_recipe(list(paths), outputdir, template=tpl)
                if src and "RECIPE_SRC" not in r:
                    r["RECIPE_SRC"] = src
                if i > 0 and chained:
                    r["inputs"] = []        # filled from the previous recipe's outputs at run time
                lost: list[tuple[dict, str, bool]] = []
                for step in r.get("steps", []):
                    calibs = step.get("calibs") or {}
                    for label, spec in list(calibs.items()):
                        if not (isinstance(spec, str) and "AUTOMATIC" in spec.upper()):
                            continue
                        try:
                            calibs[label] = db.get_calib(ref, cdata.datatypes[label]).filepath
                        except (ValueError, KeyError):
                            lost.append((step, label, "OPTIONAL" in spec.upper()))
                required = [(st, lab) for st, lab, optional in lost if not optional]
                if not required:
                    for st, lab, _opt in lost:
                        st["calibs"][lab] = None
                    if had_jit:
                        r["drpconfig"]["jit_calib_id"] = old_jit
                    else:
                        r["drpconfig"].pop("jit_calib_id", None)
                rec = Recipe.from_dict(r)
                rec.unresolved = [(st["name"], lab) for st, lab in required]
                built.append(rec)
            out.append(built)
    return out, log.getvalue()


# --------------------------------------------------------------------------- #
# full analysis of a set of files (what the Parser window runs in a thread)
# --------------------------------------------------------------------------- #
@dataclass
class GroupAnalysis:
    group: FileGroup
    recommendation: Recommendation | None = None
    chains: list[list[Recipe]] | None = None   # populated recipes, if they could be built
    status: str = "pending"                    # pending | working | ok | warn | incomplete | err
    message: str = ""
    override: str | None = None                # template name or path chosen by the user
    log: str = ""

    @property
    def recipe_summary(self) -> str:
        if self.chains:
            return " | ".join(" → ".join(r.name for r in chain) for chain in self.chains)
        if self.recommendation:
            return self.recommendation.summary()
        return ""

    def flat_recipes(self) -> list[Recipe]:
        return [r for chain in (self.chains or []) for r in chain]

    @property
    def missing_calibs(self) -> list[tuple[str, str, str]]:
        """(recipe, step, calib label) for required calibrations the CalDB could not supply."""
        return [(r.name, st, lab) for r in self.flat_recipes() for st, lab in r.unresolved]

    @property
    def incomplete(self) -> bool:
        return bool(self.missing_calibs)

    @property
    def complete(self) -> bool:
        """Has recipes and every required calibration resolved."""
        return bool(self.chains) and not self.incomplete


def _calib_report(chains: list[list[Recipe]]) -> tuple[int, int]:
    """(optional calibs resolved to None, steps marked skip) across populated recipes."""
    optional_missing = 0
    skipped = 0
    for chain in chains:
        for r in chain:
            for s in r.steps:
                if s.skip:
                    skipped += 1
                for label, spec in (s.calibs or {}).items():
                    if spec is None:
                        optional_missing += 1
    return optional_missing, skipped


def _calib_context(exc: BaseException) -> tuple[str | None, str | None, str | None] | None:
    """(step, calib label, file) when ``exc`` was raised while the walker resolved a
    calibration (``_fill_in_calib_files``), else None."""
    step = calib = path = None
    inside = False
    for frame, _lineno in traceback.walk_tb(exc.__traceback__):
        fn = frame.f_code.co_name
        loc = frame.f_locals
        if fn == "_fill_in_calib_files":
            inside = True
            st = loc.get("step")
            step = st.get("name") if isinstance(st, dict) else None
            calib = loc.get("calib")
        elif fn == "get_calib" and "calib_filepath" in loc:
            path = loc.get("calib_filepath")
    return (step, calib, path) if inside else None


def describe_exception(exc: BaseException) -> str:
    """One line for the group table: the exception plus, when it happened while the walker
    was resolving a calibration, which step, calibration type and file were involved."""
    head = f"{type(exc).__name__}: {exc}".strip().splitlines()[0][:300]
    ctx = _calib_context(exc)
    if ctx and (ctx[0] or ctx[1]):
        step, calib, path = ctx
        text = f"while resolving {calib or 'a calibration'}"
        if step:
            text += f" for {step}"
        if path:
            text += f" ({path})"
        return f"{head} {text}"
    return head


def _missing_message(missing: list[tuple[str, str, str]]) -> str:
    _recipe, step, label = missing[0]
    more = f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""
    return f"incomplete: no {label} in the CalDB for {step}{more}"


def analyze_group(group: FileGroup, outputdir: str, override: str | None = None) -> GroupAnalysis:
    """Recommend (or apply an override) and build populated recipes for one group."""
    ga = GroupAnalysis(group, override=override)
    if group.key[0] == "unreadable":
        ga.status, ga.message = "err", "files could not be read as FITS"
        return ga
    if override:
        ga.recommendation = Recommendation([[os.path.basename(override)]], False, compute_facts(group.files),
                                           reason=f"chosen manually: {os.path.basename(override)}")
    else:
        ga.recommendation = recommend(group)
        if not ga.recommendation.ok:
            ga.status = "err"
            ga.message = (ga.recommendation.error or "no recommendation").strip().splitlines()[-1]
            return ga
    try:
        ga.chains, ga.log = build_recipes(group.paths, outputdir, template=override)
    except Exception as exc:  # noqa: BLE001 - corgidrp raises ValueError for missing calibs, among others
        first_tb = traceback.format_exc()
        if _calib_context(exc) is None:
            ga.status, ga.message, ga.log = "err", describe_exception(exc), first_tb
            return ga
        # a calibration is missing: still make the recipe, with the gap left as AUTOMATIC
        if override:
            specs, chained = [[override]], False
        else:
            specs, chained = ga.recommendation.chains, ga.recommendation.chained
        try:
            ga.chains, log2 = build_incomplete_recipes(group.paths, outputdir, specs, chained)
        except Exception:  # noqa: BLE001
            ga.status, ga.message = "err", describe_exception(exc)
            ga.log = first_tb + "\nbuilding an incomplete recipe failed too:\n" + traceback.format_exc()
            ga.chains = None
            return ga
        ga.log = (f"autogen_recipe stopped on a missing calibration:\n{first_tb}\n"
                  f"rebuilt with the missing calibrations left as AUTOMATIC:\n{log2}")
    if group.batch is not None and group.batch.slug:
        for chain in ga.chains:
            for r in chain:
                r.name = batch_recipe_name(r.name, group.batch)
    if ga.incomplete:
        ga.status, ga.message = "incomplete", _missing_message(ga.missing_calibs)
        return ga
    missing, skipped = _calib_report(ga.chains)
    if skipped:
        ga.status, ga.message = "warn", f"{skipped} step{'s' if skipped != 1 else ''} will be skipped (missing calibration)"
    elif missing:
        ga.status, ga.message = "warn", f"{missing} optional calibration{'s' if missing != 1 else ''} missing"
    else:
        n = sum(len(c) for c in ga.chains)
        ga.status, ga.message = "ok", f"calibrations resolved · {n} recipe{'s' if n != 1 else ''}"
    return ga


def analyze(records: list[FileRecord], outputdir: str, overrides: dict[tuple, str] | None = None,
            progress=None, split: SplitSpec | None = None) -> list[GroupAnalysis]:
    """Group the records, split each group per ``split`` and analyze every batch.

    ``overrides`` is keyed by :attr:`FileGroup.override_key` (an unsplit group also accepts its
    plain :attr:`FileGroup.key`). ``progress(i, n, label)`` per batch.
    """
    overrides = overrides or {}
    groups = [sg for g in group_files(records) for sg in split_group(g, split)]
    out = []
    for i, g in enumerate(groups):
        if progress:
            progress(i, len(groups), g.label)
        override = overrides.get(g.override_key, overrides.get(g.key) if g.batch is None else None)
        out.append(analyze_group(g, outputdir, override))
    if progress:
        progress(len(groups), len(groups), "done")
    return out
