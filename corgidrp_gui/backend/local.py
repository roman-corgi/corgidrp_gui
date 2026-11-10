"""Local filesystem backend (the only one for now)."""
from __future__ import annotations

import os
import re
import sys

from .base import Backend, FileEntry, FitsHeaders


class LocalBackend(Backend):
    name = "local"

    def listdir(self, path, pattern="*"):
        import fnmatch
        out = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    if not e.is_dir() and not fnmatch.fnmatch(e.name, pattern):
                        continue
                    try:
                        st = e.stat()
                        out.append(FileEntry(e.path, e.name, e.is_dir(), st.st_size, st.st_mtime))
                    except OSError:
                        out.append(FileEntry(e.path, e.name, e.is_dir()))
        except OSError:
            return []
        return sorted(out, key=lambda f: (not f.is_dir, f.name.lower()))

    def exists(self, path):
        return os.path.exists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def read_headers(self, path):
        try:
            from astropy.io import fits
            with fits.open(path, memmap=True, lazy_load_hdus=True) as hdul:
                pri = _header_to_dict(hdul[0].header)
                ext = _header_to_dict(hdul[1].header) if len(hdul) > 1 else {}
            return FitsHeaders(path, pri, ext)
        except Exception as exc:  # noqa: BLE001
            return FitsHeaders(path, error=f"{type(exc).__name__}: {exc}")

    def python_executable(self):
        return sys.executable

    def home(self):
        return os.path.expanduser("~")


_RECIPE_CARD = re.compile(r"^RECIPE\d*$")
_RECIPE_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')


def _header_to_dict(header) -> dict:
    """Header cards as a dict, skipping COMMENT/HISTORY.

    corgidrp writes each recipe a file went through into RECIPE, RECIPE2, ... as JSON
    long strings spread over hundreds of CONTINUE cards; parsing those values dominates
    header reading (about 25x slower). For those keywords only the recipe's name is kept,
    taken from the first card image.
    """
    d = {}
    for card in header.cards:
        k = card.keyword
        if not k or k in ("COMMENT", "HISTORY"):
            continue
        if _RECIPE_CARD.match(k):
            image = getattr(card, "_image", None) or ""
            if len(image) > 80:
                m = _RECIPE_NAME.search(image[:80])
                if m:
                    d[k] = m.group(1)
                    continue
        d[k] = card.value
    return d
