"""Recipe model: a faithful, editable in-memory form of corgidrp's recipe JSON.

A recipe round-trips through :meth:`Recipe.to_dict` without losing keys the
GUI does not understand (``ram_heavy``, ``process_in_chunks``, ``RECIPE_SRC``
and anything future versions add).
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

from .steps import MISSING, SAVE_STEP_NAME, StepCatalog, StepInfo

__all__ = ["RecipeStep", "Recipe", "Issue", "validate_recipe"]

_RECIPE_KEYS = ("name", "template", "drpconfig", "inputs", "outputdir", "steps")
_STEP_KEYS = ("name", "calibs", "keywords", "skip")


@dataclass
class RecipeStep:
    name: str
    calibs: dict[str, str | None] | None = None
    keywords: dict[str, Any] | None = None
    skip: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "RecipeStep":
        d = dict(d)
        return cls(
            name=d.pop("name"),
            calibs=dict(d.pop("calibs")) if "calibs" in d and d["calibs"] is not None else d.pop("calibs", None),
            keywords=dict(d.pop("keywords")) if "keywords" in d and d["keywords"] is not None else d.pop("keywords", None),
            skip=d.pop("skip", None),
            extra=d,
        )

    @classmethod
    def new(cls, info: StepInfo) -> "RecipeStep":
        """A fresh step carrying what templates usually set for it."""
        calibs = info.default_calibs() or None
        keywords = info.default_keywords() or None
        return cls(name=info.name, calibs=calibs, keywords=keywords)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"name": self.name}
        if self.calibs is not None:
            d["calibs"] = dict(self.calibs)
        if self.keywords is not None:
            d["keywords"] = copy.deepcopy(self.keywords)
        if self.skip is not None:
            d["skip"] = self.skip
        d.update(self.extra)
        return d

    def copy(self) -> "RecipeStep":
        return RecipeStep.from_dict(copy.deepcopy(self.to_dict()))

    # convenience
    def set_keyword(self, key: str, value: Any):
        if self.keywords is None:
            self.keywords = {}
        self.keywords[key] = value

    def unset_keyword(self, key: str):
        if self.keywords and key in self.keywords:
            del self.keywords[key]
            if not self.keywords:
                self.keywords = None


@dataclass
class Recipe:
    name: str = "new_recipe"
    template: bool = False
    drpconfig: dict[str, Any] = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    outputdir: str = ""
    steps: list[RecipeStep] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None          # where it was loaded from, if anywhere
    #: (step name, calib label) pairs that no CalDB entry matched when the Parser made this
    #: recipe; they are still "AUTOMATIC" in the recipe. Not written to JSON.
    unresolved: list[tuple[str, str]] = field(default_factory=list)

    # -- (de)serialisation -------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict, source_path: str | None = None) -> "Recipe":
        d = dict(d)
        return cls(
            name=d.pop("name", "recipe"),
            template=bool(d.pop("template", False)),
            drpconfig=dict(d.pop("drpconfig", {}) or {}),
            inputs=list(d.pop("inputs", []) or []),
            outputdir=d.pop("outputdir", "") or "",
            steps=[RecipeStep.from_dict(s) for s in d.pop("steps", [])],
            extra=d,
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: str) -> "Recipe":
        with open(path) as f:
            return cls.from_dict(json.load(f), source_path=path)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "template": self.template,
            "drpconfig": copy.deepcopy(self.drpconfig),
            "inputs": list(self.inputs),
            "outputdir": self.outputdir,
            "steps": [s.to_dict() for s in self.steps],
        }
        # keep unknown keys, in their original position where that was before 'steps'
        for k, v in self.extra.items():
            d[k] = copy.deepcopy(v)
        return d

    def to_json(self, indent: int = 4) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write(self.to_json())
            f.write("\n")
        self.source_path = path

    def copy(self) -> "Recipe":
        r = Recipe.from_dict(copy.deepcopy(self.to_dict()), self.source_path)
        r.unresolved = list(self.unresolved)
        return r

    # -- editing helpers ----------------------------------------------------
    @property
    def step_names(self) -> list[str]:
        return [s.name for s in self.steps]

    def insert_step(self, index: int, step: RecipeStep):
        self.steps.insert(index, step)

    def move_step(self, old: int, new: int):
        s = self.steps.pop(old)
        self.steps.insert(new, s)

    def remove_step(self, index: int) -> RecipeStep:
        return self.steps.pop(index)

    def as_template(self, name: str | None = None) -> "Recipe":
        """A copy with inputs/outputdir cleared and template=True, ready for user_templates."""
        t = self.copy()
        t.template = True
        t.inputs = []
        t.outputdir = ""
        t.extra.pop("RECIPE_SRC", None)
        if name:
            t.name = name
        return t


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    severity: str                  # "error" | "warning"
    message: str
    step_index: int | None = None  # None = whole recipe

    def __str__(self):
        where = f"step {self.step_index + 1}: " if self.step_index is not None else ""
        return f"{self.severity}: {where}{self.message}"


def validate_recipe(recipe: Recipe, catalog: StepCatalog, start_level: str | None = None,
                    check_inputs: bool = False) -> list[Issue]:
    """Static checks a recipe should pass before it is queued.

    Errors are things the walker will certainly reject or crash on; warnings
    are deviations from what the shipped templates do.
    """
    issues: list[Issue] = []
    if not recipe.steps:
        issues.append(Issue("error", "recipe has no steps"))
    if not recipe.name or not recipe.name.strip():
        issues.append(Issue("error", "recipe has no name"))

    for i, step in enumerate(recipe.steps):
        info = catalog.get(step.name)
        if info is None:
            issues.append(Issue("error", f"unknown step '{step.name}'", i))
            continue
        # keywords must exist in the signature (unless **kwargs)
        for k in (step.keywords or {}):
            if step.name == SAVE_STEP_NAME:
                if info.arg(k) is None:
                    issues.append(Issue("warning", f"save ignores keyword '{k}' (knows: suffix, ram_heavy_save)", i))
            elif info.arg(k) is None and not info.accepts_var_kwargs:
                if any(c.param == k for c in info.calibs):
                    issues.append(Issue("error", f"'{k}' is a calibration argument of {step.name}; set it under 'calibs', not 'keywords'", i))
                else:
                    issues.append(Issue("error", f"{step.name} has no keyword argument '{k}'", i))
        # required (non-default, non-calib) args must be given
        for a in info.args:
            if a.required and (step.keywords is None or a.name not in step.keywords):
                issues.append(Issue("error", f"{step.name} requires keyword '{a.name}'", i))
        # calibs: labels must be datatypes; count must not exceed slots; order matters
        calibs = step.calibs or {}
        for j, (label, spec) in enumerate(calibs.items()):
            if label not in catalog.datatypes:
                issues.append(Issue("error", f"'{label}' is not a corgidrp calibration type", i))
            if j >= len(info.calibs):
                issues.append(Issue("error", f"{step.name} takes {len(info.calibs)} calibration product(s); '{label}' is number {j + 1}", i))
            elif label not in info.calibs[j].datatypes:
                issues.append(Issue("warning", f"slot {j + 1} of {step.name} ({info.calibs[j].param}) is usually {'/'.join(info.calibs[j].datatypes)}, not {label}", i))
            if spec is None or (isinstance(spec, str) and spec.strip() == ""):
                issues.append(Issue("warning", f"calibration '{label}' is empty (None); the step will get no {label}", i))
            elif isinstance(spec, str) and "AUTOMATIC" not in spec.upper() and not os.path.exists(spec):
                issues.append(Issue("warning", f"calibration file for {label} does not exist: {spec}", i))
            elif (isinstance(spec, str) and "AUTOMATIC" in spec.upper() and "OPTIONAL" not in spec.upper()
                  and (step.name, label) in recipe.unresolved):
                how = ("it is looked up again when the step runs, so it works only if an earlier job makes one"
                       if recipe.drpconfig.get("jit_calib_id") else "choose a file")
                issues.append(Issue("warning", f"no {label} in the CalDB matched these files; {how}", i))
        if info.calibs and not calibs and step.name != SAVE_STEP_NAME:
            needed = [c for c in info.calibs if not c.optional_in_templates]
            if needed:
                issues.append(Issue("warning", f"{step.name} usually takes {', '.join(c.label for c in needed)} under 'calibs'", i))

    for idx, msg in catalog.order_warnings(recipe.step_names, start_level):
        issues.append(Issue("warning", msg, idx))

    if check_inputs:
        missing = [p for p in recipe.inputs if not os.path.exists(p)]
        if missing:
            issues.append(Issue("error", f"{len(missing)} input file(s) do not exist, e.g. {missing[0]}"))
        if recipe.outputdir and not os.path.isdir(recipe.outputdir):
            issues.append(Issue("warning", f"output directory does not exist yet: {recipe.outputdir}"))
    return issues


_NAME_LEVEL = __import__("re").compile(r"^(l1|l2a|l2b|l3|l4)_", __import__("re").I)


def guess_start_level(recipe: "Recipe", backend=None) -> str | None:
    """Data level of the recipe's inputs: from the first input's DATALVL header if
    readable, else from a name like ``l2a_to_l2b``."""
    if recipe.inputs:
        try:
            if backend is None:
                from ..backend import default_backend
                backend = default_backend()
            h = backend.read_headers(recipe.inputs[0])
            lvl = h.get("DATALVL")
            if lvl:
                return str(lvl)
        except Exception:  # noqa: BLE001 - header read is best effort
            pass
    for text in (recipe.name, os.path.basename(recipe.source_path or "")):
        m = _NAME_LEVEL.match(text or "")
        if m:
            return m.group(1).upper().replace("L2A", "L2a").replace("L2B", "L2b")
    return None
