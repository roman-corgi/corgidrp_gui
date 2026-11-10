"""Dataset summary: what is in a pile of FITS files, grouped by the keywords corgidrp cares about.

Qt-free. Works on :class:`~corgidrp_gui.core.parser.FileRecord` (headers only). The keyword
catalogue below lists the header keywords that steer corgidrp: the ones
:func:`corgidrp.walker.guess_template` branches on, the ones the processing steps split
datasets by (``split_dataset`` calls in l2a_to_l2b, l3_to_l4, darks, combine, ...), and the
observation and instrument settings that tell frames apart.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .parser import FileRecord

__all__ = [
    "KeyInfo", "KEYWORDS", "KEY_INFO", "CATEGORIES", "PROCESSING", "PRESETS", "DEFAULT_PRESET",
    "value_of", "fmt_value", "describe_value", "SummaryNode", "build_tree", "KeywordStat",
    "keyword_stats", "varying_keywords", "time_span", "total_exptime", "summary_text",
    "parse_hierarchy",
]

#: pseudo-keyword: the chain of recipes a file went through (RECIPE, RECIPE2, ...)
PROCESSING = "PROCESSING"


@dataclass(frozen=True)
class KeyInfo:
    key: str
    category: str
    why: str


KEYWORDS: list[KeyInfo] = [
    # what guess_template branches on
    KeyInfo("DATALVL", "Recipe selection", "processing level; guess_template branches on it first"),
    KeyInfo("VISTYPE", "Recipe selection", "visit type; picks calibration vs science templates"),
    KeyInfo("DPAMNAME", "Recipe selection", "IMAGING, PRISMn (spectroscopy) or POLnn (polarimetry); selects the template family"),
    KeyInfo("ISPC", "Recipe selection", "photon counting (1) or analog (0); L2a data are split on it"),
    KeyInfo("FPAMNAME", "Recipe selection", "focal plane mask; ND filters pick ND calibrations, OPEN/ND frames are skipped in L3→L4"),
    KeyInfo("FSMX", "Recipe selection", "FSM offsets; differing positions select polarimetric calibration"),
    KeyInfo("FSMY", "Recipe selection", "FSM offsets; differing positions select polarimetric calibration"),
    # observation: what L3→L4 splits and matches on
    KeyInfo("VISITID", "Observation", "visit; L3→L4 steps split and match frames per visit"),
    KeyInfo("TARGET", "Observation", "target name; L3→L4 splits by target"),
    KeyInfo("PSFREF", "Observation", "0 = science target, 1 = PSF reference star (RDI)"),
    KeyInfo("SATSPOTS", "Observation", "satellite-spot frames, used to find the star"),
    KeyInfo("OBSNAME", "Observation", "observation name; nonlinearity calibration splits on it"),
    KeyInfo("PA_APER", "Observation", "aperture position angle (roll), for ADI"),
    KeyInfo("ROLL", "Observation", "spacecraft roll"),
    KeyInfo("PROGNUM", "Observation", "program number"),
    KeyInfo("CPGSFILE", "Observation", "CPGS campaign file"),
    KeyInfo("AUXFILE", "Observation", "auxiliary file; sorting splits on it"),
    # detector settings: the (EXPTIME, EMGAIN_C, KGAINPAR) split of darks and l2a_to_l2b
    KeyInfo("EXPTIME", "Detector", "exposure time; darks and dark subtraction split on it"),
    KeyInfo("EMGAIN_C", "Detector", "commanded EM gain; darks, gain and nonlinearity steps split on it"),
    KeyInfo("KGAINPAR", "Detector", "k gain; darks split on it"),
    KeyInfo("OPMODE", "Detector", "detector operating mode"),
    KeyInfo("PHTCNT", "Detector", "photon counting requested"),
    KeyInfo("ARRTYPE", "Detector", "array type (SCI, ENG)"),
    KeyInfo("FRMTYPE", "Detector", "frame type"),
    # optics
    KeyInfo("CFAMNAME", "Optics", "color filter (band); L3→L4 splits by band"),
    KeyInfo("LSAMNAME", "Optics", "Lyot stop"),
    KeyInfo("FSAMNAME", "Optics", "field stop"),
    KeyInfo("SPAMNAME", "Optics", "shaped pupil"),
    KeyInfo("FSMPRFL", "Optics", "FSM profile"),
    # flags
    KeyInfo("ISACQ", "Flags", "acquisition frame; discard_setup_frames removes these"),
    KeyInfo("SPBAL", "Flags", "set-point balancing frame"),
    KeyInfo("ISFLAT", "Flags", "flat frame"),
    KeyInfo("ISHOWFSC", "Flags", "HOWFSC frame"),
    KeyInfo("IS_BAD", "Flags", "frame marked bad"),
    KeyInfo("MOCK", "Flags", "simulated data"),
    # processing state
    KeyInfo(PROCESSING, "Processing", "the recipes these files went through (RECIPE, RECIPE2, ...)"),
    KeyInfo("DATATYPE", "Processing", "calibration product type"),
    KeyInfo("BUNIT", "Processing", "data units"),
    KeyInfo("DRPVERSN", "Processing", "corgidrp version that wrote the file"),
]
KEY_INFO: dict[str, KeyInfo] = {k.key: k for k in KEYWORDS}
CATEGORIES: list[str] = list(dict.fromkeys(k.category for k in KEYWORDS))

#: name -> grouping hierarchy
PRESETS: dict[str, list[str]] = {
    "Recipe selection, then visit": ["DATALVL", "VISTYPE", "DPAMNAME", "ISPC", "CFAMNAME", "VISITID", "PSFREF", "SATSPOTS"],
    "Visit": ["VISITID", "PSFREF", "SATSPOTS", "PA_APER"],
    "Target and roll": ["TARGET", "PSFREF", "PA_APER", "SATSPOTS"],
    "Detector settings": ["DATALVL", "VISTYPE", "EXPTIME", "EMGAIN_C", "KGAINPAR", "ISPC"],
    "Optics": ["CFAMNAME", "FPAMNAME", "LSAMNAME", "FSAMNAME", "DPAMNAME"],
    "Processing": ["DATALVL", PROCESSING, "DRPVERSN"],
}
DEFAULT_PRESET = "Recipe selection, then visit"

#: keywords never worth summarising (structure, per-file identity, WCS)
_SKIP = {"SIMPLE", "BITPIX", "NAXIS", "EXTEND", "XTENSION", "PCOUNT", "GCOUNT", "EXTNAME", "FILENAME",
         "CHECKSUM", "DATASUM", "BZERO", "BSCALE", "COMMENT", "HISTORY"}
_SKIP_RE = re.compile(r"^(NAXIS\d+|RECIPE\d*|C(D|RPIX|RVAL|TYPE|UNIT|DELT)\d(_\d)?|PC\d_\d)$")
#: per-frame timing keywords, summarised as a span instead of value counts
_TIME_KEYS = ("DATETIME", "SCTSRT", "FTIMEUTC", "DATE-OBS")


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
def _recipe_name(v) -> str:
    s = str(v)
    if s.startswith("{"):
        try:
            return str(json.loads(s).get("name", "?"))
        except (ValueError, AttributeError):
            m = re.search(r'"name"\s*:\s*"([^"]+)"', s)
            return m.group(1) if m else "?"
    return s


def value_of(rec: FileRecord, key: str):
    """Header value (extension first, then primary), or a derived value for pseudo-keywords."""
    if key == PROCESSING:
        cached = rec.__dict__.get("_processing")
        if cached is None:
            h = rec.headers
            names = {}
            for k in list(h.ext) + list(h.pri):
                if k.startswith("RECIPE"):
                    m = re.match(r"^RECIPE(\d*)$", k)
                    if m:
                        names[int(m.group(1) or 1)] = _recipe_name(h.get(k))
            cached = " → ".join(names[i] for i in sorted(names)) if names else "(raw)"
            rec.__dict__["_processing"] = cached
        return cached
    v = rec.value(key)
    if isinstance(v, str):
        v = v.strip()
    return v


def fmt_value(v) -> str:
    if v is None:
        return "(absent)"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return f"{v:.6g}"
    s = str(v)
    return s if s else "(empty)"


def _sort_key(v):
    """Values of one keyword in a sensible order; absent last."""
    if v is None:
        return (2, 0, "")
    if isinstance(v, (bool, int, float)):
        return (0, float(v), "")
    return (1, 0, str(v))


def describe_value(key: str, value) -> str:
    """A few words saying what a value means, or ''."""
    if value is None:
        return ""
    s = str(value).strip()
    if key == "PSFREF":
        return {"0": "science target", "1": "PSF reference", "False": "science target", "True": "PSF reference"}.get(s, "")
    if key == "SATSPOTS":
        return {"1": "satellite spots", "True": "satellite spots", "0": "no satellite spots", "False": "no satellite spots"}.get(s, "")
    if key == "ISPC":
        return {"1": "photon counting", "0": "analog", "True": "photon counting", "False": "analog"}.get(s, "")
    if key == "DPAMNAME":
        if s.startswith("PRISM"):
            return "spectroscopy"
        if s.startswith("POL"):
            return "polarimetry"
        return "imaging" if s == "IMAGING" else ""
    if key == "FPAMNAME":
        if s.startswith("ND"):
            return "ND filter"
        if s.startswith("OPEN"):
            return "unocculted"
        return ""
    if key == "VISTYPE":
        if s.startswith("CGIVST_CAL_"):
            return "calibration visit"
        if s.startswith("CGIVST_TDD"):
            return "technology demonstration"
        if s.startswith("CGIVST_ENG"):
            return "engineering"
        return ""
    if key in ("ISACQ", "ISFLAT", "SPBAL", "ISHOWFSC", "IS_BAD", "MOCK"):
        return "yes" if s in ("1", "True") else ("no" if s in ("0", "False") else "")
    return ""


def _parse_time(v) -> _dt.datetime | None:
    if v is None:
        return None
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def time_span(records: Iterable[FileRecord]) -> tuple[_dt.datetime, _dt.datetime] | None:
    """Earliest and latest observation time (DATETIME, else SCTSRT, FTIMEUTC, DATE-OBS)."""
    times = []
    for r in records:
        for k in _TIME_KEYS:
            t = _parse_time(r.value(k))
            if t is not None:
                times.append(t)
                break
    return (min(times), max(times)) if times else None


def fmt_span(span) -> str:
    if not span:
        return ""
    a, b = span
    if a == b:
        return a.strftime("%Y-%m-%d %H:%M:%S")
    if a.date() == b.date():
        return f"{a:%Y-%m-%d %H:%M} – {b:%H:%M}"
    return f"{a:%Y-%m-%d %H:%M} – {b:%Y-%m-%d %H:%M}"


def total_exptime(records: Iterable[FileRecord]) -> float:
    total = 0.0
    for r in records:
        try:
            total += float(r.value("EXPTIME") or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def fmt_seconds(s: float) -> str:
    if s <= 0:
        return ""
    if s < 120:
        return f"{s:.4g} s"
    if s < 7200:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.2f} h"


def parse_hierarchy(text: str) -> list[str]:
    """'VISITID > PSFREF, satspots' -> ['VISITID', 'PSFREF', 'SATSPOTS']."""
    parts = re.split(r"[,>›/\s]+", text or "")
    out: list[str] = []
    for p in parts:
        p = p.strip().upper()
        if p and p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# grouping tree
# --------------------------------------------------------------------------- #
@dataclass
class SummaryNode:
    """A set of files sharing the values of the keywords on the path from the root."""
    records: list[FileRecord]
    key: str | None = None                     #: keyword this node split on (None for the root)
    value: Any = None
    shared: list[tuple[str, Any]] = field(default_factory=list)   #: folded levels: same value for every file here
    children: list["SummaryNode"] = field(default_factory=list)
    parent: "SummaryNode | None" = field(default=None, repr=False)

    @property
    def n_files(self) -> int:
        return len(self.records)

    @property
    def path(self) -> list[tuple[str, Any]]:
        """Every (keyword, value) fixed for this node, from the root down."""
        out: list[tuple[str, Any]] = []
        n: SummaryNode | None = self
        chain = []
        while n is not None:
            chain.append(n)
            n = n.parent
        for node in reversed(chain):
            if node.key is not None:
                out.append((node.key, node.value))
            out.extend(node.shared)
        return out

    @property
    def title(self) -> str:
        if self.key is None:
            return f"All files"
        return _pair_text(self.key, self.value)

    @property
    def shared_text(self) -> str:
        return " · ".join(_pair_text(k, v) for k, v in self.shared)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def _pair_text(key, value) -> str:
    note = describe_value(key, value)
    return f"{key} = {fmt_value(value)}" + (f" ({note})" if note else "")


def build_tree(records: list[FileRecord], hierarchy: list[str], fold_uniform: bool = True,
               value_fn: Callable[[FileRecord, str], Any] = value_of) -> SummaryNode:
    """Group ``records`` level by level along ``hierarchy``.

    With ``fold_uniform``, a level where every file of a node has the same value makes no
    child; the (keyword, value) pair is recorded in the node's ``shared`` list instead, so the
    tree only branches where the data actually differ. Unreadable files form their own branch.
    """
    readable = [r for r in records if r.ok]
    bad = [r for r in records if not r.ok]
    root = SummaryNode(list(records))
    _split(root, readable, list(hierarchy), fold_uniform, value_fn)
    if bad:
        root.children.append(SummaryNode(bad, key="unreadable", value=f"{len(bad)} files", parent=root))
    return root


def _split(node: SummaryNode, records: list[FileRecord], levels: list[str], fold: bool, value_fn):
    while levels:
        key, levels = levels[0], levels[1:]
        buckets: dict[Any, list[FileRecord]] = {}
        for r in records:
            v = value_fn(r, key)
            try:
                hash(v)
            except TypeError:
                v = str(v)
            buckets.setdefault(v, []).append(r)
        if fold and len(buckets) == 1:
            node.shared.append((key, next(iter(buckets))))
            continue
        for v in sorted(buckets, key=_sort_key):
            child = SummaryNode(buckets[v], key=key, value=v, parent=node)
            node.children.append(child)
            _split(child, buckets[v], list(levels), fold, value_fn)
        return


# --------------------------------------------------------------------------- #
# keyword statistics
# --------------------------------------------------------------------------- #
@dataclass
class KeywordStat:
    key: str
    category: str
    why: str
    counts: list[tuple[Any, int]]       #: (value, number of files), most common first
    n_files: int

    @property
    def n_unique(self) -> int:
        return len(self.counts)

    @property
    def constant(self) -> bool:
        return self.n_unique <= 1

    @property
    def absent(self) -> bool:
        return self.n_unique == 1 and self.counts[0][0] is None

    @property
    def per_file(self) -> bool:
        """Every file has its own value (timestamps and the like)."""
        return self.n_files > 2 and self.n_unique == self.n_files

    def values_text(self, limit: int = 8) -> str:
        if self.per_file:
            vals = sorted((v for v, _ in self.counts if v is not None), key=_sort_key)
            return f"different in every file ({fmt_value(vals[0])} … {fmt_value(vals[-1])})" if vals else ""
        parts = []
        for v, n in self.counts[:limit]:
            note = describe_value(self.key, v)
            parts.append(f"{fmt_value(v)}{f' ({note})' if note else ''} ×{n}")
        if self.n_unique > limit:
            parts.append(f"… {self.n_unique - limit} more")
        return ", ".join(parts)


def _stat(records: list[FileRecord], key: str, value_fn=value_of) -> KeywordStat:
    c: Counter = Counter()
    for r in records:
        v = value_fn(r, key)
        try:
            hash(v)
        except TypeError:
            v = str(v)
        c[v] += 1
    counts = sorted(c.items(), key=lambda kv: (-kv[1], _sort_key(kv[0])))
    info = KEY_INFO.get(key)
    return KeywordStat(key, info.category if info else "Other", info.why if info else "", counts, len(records))


def keyword_stats(records: list[FileRecord], keys: list[str] | None = None) -> list[KeywordStat]:
    """Value counts of each keyword (default: the catalogue) over the readable records."""
    ok = [r for r in records if r.ok]
    return [_stat(ok, k) for k in (keys or [k.key for k in KEYWORDS])]


def varying_keywords(records: list[FileRecord]) -> list[KeywordStat]:
    """Every header keyword whose value is not the same in all readable files, catalogue keywords
    first, then the rest alphabetically. Structural and WCS keywords are left out."""
    ok = [r for r in records if r.ok]
    keys: set[str] = set()
    for r in ok:
        keys.update(r.headers.pri)
        keys.update(r.headers.ext)
    keys = {k for k in keys if k not in _SKIP and not _SKIP_RE.match(k)}
    known = [k.key for k in KEYWORDS if k.key in keys or k.key == PROCESSING]
    rest = sorted(keys - set(known))
    out = []
    for k in known + rest:
        st = _stat(ok, k)
        if not st.constant:
            out.append(st)
    return out


# --------------------------------------------------------------------------- #
# text export
# --------------------------------------------------------------------------- #
def summary_text(records: list[FileRecord], hierarchy: list[str], fold_uniform: bool = True) -> str:
    """Plain-text summary: totals, the grouping tree, and which catalogue keywords vary."""
    root = build_tree(records, hierarchy, fold_uniform)
    ok = [r for r in records if r.ok]
    lines = [f"{len(records)} files" + (f" ({len(records) - len(ok)} unreadable)" if len(ok) != len(records) else "")
             + (f", {fmt_span(time_span(ok))}" if time_span(ok) else "")
             + (f", {fmt_seconds(total_exptime(ok))} total exposure" if total_exptime(ok) else "")]
    if root.shared:
        lines.append("All files: " + root.shared_text)
    lines.append("Grouped by: " + " › ".join(hierarchy))
    for node in root.walk():
        if node is root:
            continue
        depth = len([1 for _ in _ancestors(node)]) - 1
        extra = f" · {node.shared_text}" if node.shared else ""
        lines.append(f"{'  ' * depth}- {node.title}: {node.n_files} files{extra}")
    stats = keyword_stats(ok)
    varying = [s for s in stats if not s.constant]
    constant = [s for s in stats if s.constant and not s.absent]
    if varying:
        lines.append("Varies:")
        lines += [f"  {s.key}: {s.values_text()}" for s in varying]
    if constant:
        lines.append("Same everywhere: " + ", ".join(f"{s.key}={fmt_value(s.counts[0][0])}" for s in constant))
    return "\n".join(lines)


def _ancestors(node: SummaryNode):
    n = node
    while n is not None:
        yield n
        n = n.parent
