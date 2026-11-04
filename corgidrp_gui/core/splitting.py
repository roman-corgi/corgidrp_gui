"""Split one input file list into several recipes.

Two independent rules, applied in this order:

1. **by keywords**: files whose headers differ in any of the listed keywords
   (VISITID, DPAMNAME, ...) go into different recipes;
2. **max files**: a batch with more than ``max_files`` files is cut into parts of
   at most that many, to bound the memory a single recipe needs.

Both the Parser and the Builder use this; it knows nothing about Qt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

__all__ = ["SplitSpec", "Batch", "split_items", "split_records", "split_paths", "batch_recipe_name"]


@dataclass
class SplitSpec:
    max_files: int = 0                              #: 0 means no limit
    keywords: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.max_files > 0 or bool(self.keywords)

    @staticmethod
    def parse_keywords(text: str) -> list[str]:
        """``"visitid, DPAMNAME"`` -> ``["VISITID", "DPAMNAME"]`` (order kept, duplicates dropped)."""
        out: list[str] = []
        for tok in re.split(r"[,\s;]+", text or ""):
            k = tok.strip().upper()
            if k and k not in out:
                out.append(k)
        return out

    @property
    def keywords_text(self) -> str:
        return ", ".join(self.keywords)

    def describe(self) -> str:
        bits = []
        if self.keywords:
            bits.append("split by " + ", ".join(self.keywords))
        if self.max_files > 0:
            bits.append(f"at most {self.max_files} files per recipe")
        return "; ".join(bits) or "no splitting"


@dataclass
class Batch:
    items: list                                     #: the files (paths or records) of this batch
    keywords: list[str] = field(default_factory=list)
    values: tuple = ()                              #: header values, one per keyword
    part: int = 1
    n_parts: int = 1

    @property
    def key(self) -> tuple:
        return tuple(zip(self.keywords, self.values))

    @property
    def label(self) -> str:
        """Human form: ``VISITID=1234 · DPAMNAME=POL0 · part 2/3``."""
        bits = [f"{k}={'?' if v is None else v}" for k, v in zip(self.keywords, self.values)]
        if self.n_parts > 1:
            bits.append(f"part {self.part}/{self.n_parts}")
        return " · ".join(bits)

    @property
    def slug(self) -> str:
        """Filename/recipe-name safe form of :attr:`label`, empty when nothing was split."""
        bits = [f"{k}-{'unknown' if v is None else _safe(str(v))}" for k, v in zip(self.keywords, self.values)]
        if self.n_parts > 1:
            bits.append(f"part{self.part}of{self.n_parts}")
        return "_".join(bits)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9.+-]+", "-", s).strip("-") or "x"


def _sort_key(values: tuple):
    # None last, then numbers before strings, each in natural order
    return tuple((v is None, isinstance(v, str), v if isinstance(v, (int, float, str)) else str(v)) for v in values)


def split_items(items: Sequence[Any], spec: SplitSpec, value_of: Callable[[Any, str], Any]) -> list[Batch]:
    """Split ``items`` per ``spec``. ``value_of(item, keyword)`` returns the header value or None.

    Without an active spec the result is a single batch holding everything (in order).
    Keyword groups are sorted by value; within a group the original order is kept.
    """
    items = list(items)
    if not spec.active:
        return [Batch(items)]
    groups: dict[tuple, list] = {}
    for it in items:
        values = tuple(value_of(it, k) for k in spec.keywords)
        groups.setdefault(values, []).append(it)
    out: list[Batch] = []
    for values in sorted(groups, key=_sort_key):
        members = groups[values]
        n = spec.max_files
        if n > 0 and len(members) > n:
            parts = [members[i:i + n] for i in range(0, len(members), n)]
        else:
            parts = [members]
        for i, part in enumerate(parts, 1):
            out.append(Batch(part, list(spec.keywords), values, i, len(parts)))
    return out


def split_records(records, spec: SplitSpec) -> list[Batch]:
    """Split Parser :class:`~corgidrp_gui.core.parser.FileRecord` objects."""
    return split_items(records, spec, lambda r, k: r.value(k))


def split_paths(paths: Sequence[str], spec: SplitSpec, headers_of: Callable[[str], Any] | None = None) -> list[Batch]:
    """Split plain paths; ``headers_of(path)`` returns an object with ``.get(key)`` (a FitsHeaders).
    Only called when keywords are set."""
    if spec.keywords and headers_of is None:
        raise ValueError("splitting by keywords needs a headers_of(path) function")
    cache: dict[str, Any] = {}

    def value_of(p, k):
        if p not in cache:
            cache[p] = headers_of(p)
        h = cache[p]
        return h.get(k) if h is not None else None

    return split_items(paths, spec, value_of)


def batch_recipe_name(base: str, batch: Batch) -> str:
    slug = batch.slug
    return f"{base}_{slug}" if slug else base
