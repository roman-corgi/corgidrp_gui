"""Dataset summary core, on synthetic header records (no FITS files needed)."""
import json

from corgidrp_gui.backend.base import FitsHeaders
from corgidrp_gui.core import dataset as ds
from corgidrp_gui.core.parser import FileRecord


def _rec(i, visit, psfref, satspots, gain=7500.0, pa=0.0, recipes=("l1_to_l2a_basic", "l2a_to_l2b")):
    pri = {"VISITID": visit, "VISTYPE": "CGIVST_TDD_OBS", "PSFREF": psfref, "TARGET": "HIP 1", "PA_APER": pa}
    ext = {"DATALVL": "L3", "DPAMNAME": "IMAGING", "ISPC": 0, "CFAMNAME": "1F", "SATSPOTS": satspots,
           "EXPTIME": 2.0, "EMGAIN_C": gain, "DATETIME": f"2026-11-04T10:{i:02d}:00", "NAXIS1": 143,
           "RECIPE": json.dumps({"name": recipes[0], "steps": []})}
    for j, name in enumerate(recipes[1:], start=2):
        ext[f"RECIPE{j}"] = name          # what the backend keeps for long RECIPE cards
    return FileRecord(f"/data/f{i:03d}.fits", FitsHeaders(f"/data/f{i:03d}.fits", pri, ext))


def _dataset():
    recs = [_rec(i, "V1", 0, i < 2) for i in range(6)]                       # science, 2 satspot frames
    recs += [_rec(10 + i, "V2", 1, False, gain=1000.0, pa=14.0) for i in range(3)]   # reference star
    recs.append(FileRecord("/data/bad.fits", FitsHeaders("/data/bad.fits", error="OSError: truncated")))
    return recs


def test_tree_folds_uniform_levels_and_branches_where_files_differ():
    recs = _dataset()
    root = ds.build_tree(recs, ds.PRESETS[ds.DEFAULT_PRESET])
    assert root.n_files == 10
    # DATALVL, VISTYPE, DPAMNAME, ISPC, CFAMNAME agree everywhere -> folded into the root
    assert [k for k, _ in root.shared] == ["DATALVL", "VISTYPE", "DPAMNAME", "ISPC", "CFAMNAME"]
    visits = [c for c in root.children if c.key == "VISITID"]
    assert [(c.value, c.n_files) for c in visits] == [("V1", 6), ("V2", 3)]
    v1, v2 = visits
    assert v1.shared == [("PSFREF", 0)] and v2.shared == [("PSFREF", 1), ("SATSPOTS", False)]
    assert [(c.value, c.n_files) for c in v1.children] == [(False, 4), (True, 2)]
    assert v2.children == []
    assert root.children[-1].key == "unreadable" and root.children[-1].n_files == 1
    assert v1.children[1].path[-2:] == [("PSFREF", 0), ("SATSPOTS", True)]
    assert "PSF reference" in v2.shared_text and "satellite spots" in v1.children[1].title
    # without folding every level branches
    flat = ds.build_tree(recs, ["DATALVL", "VISITID"], fold_uniform=False)
    assert flat.children[0].key == "DATALVL" and flat.children[0].children[0].key == "VISITID"


def test_keyword_stats_and_varying_keywords():
    recs = _dataset()
    stats = {s.key: s for s in ds.keyword_stats(recs)}
    assert stats["PSFREF"].counts == [(0, 6), (1, 3)] and not stats["PSFREF"].constant
    assert stats["CFAMNAME"].constant and stats["CFAMNAME"].counts == [("1F", 9)]
    assert stats["ROLL"].absent
    assert stats[ds.PROCESSING].counts == [("l1_to_l2a_basic → l2a_to_l2b", 9)]
    assert "PSF reference" in stats["PSFREF"].values_text()
    varying = {s.key: s for s in ds.varying_keywords(recs)}
    assert {"VISITID", "PSFREF", "SATSPOTS", "EMGAIN_C", "PA_APER", "DATETIME"} <= set(varying)
    assert "NAXIS1" not in varying and "CFAMNAME" not in varying and "RECIPE" not in varying
    assert varying["DATETIME"].per_file and varying["DATETIME"].values_text().startswith("different in every file")


def test_values_hierarchy_and_text():
    recs = _dataset()
    assert ds.parse_hierarchy("visitid › PSFREF, satspots  VISITID") == ["VISITID", "PSFREF", "SATSPOTS"]
    assert ds.value_of(recs[0], ds.PROCESSING) == "l1_to_l2a_basic → l2a_to_l2b"
    raw = _rec(50, "V9", 0, False, recipes=("x",))
    del raw.headers.ext["RECIPE"]
    assert ds.value_of(raw, ds.PROCESSING) == "(raw)"
    assert ds.fmt_value(8.699999809265137) == "8.7" and ds.fmt_value(None) == "(absent)"
    assert ds.describe_value("FPAMNAME", "ND225") == "ND filter" and ds.describe_value("DPAMNAME", "POL0") == "polarimetry"
    span = ds.time_span(recs)
    assert span[0].minute == 0 and span[1].minute == 12
    assert ds.total_exptime(recs) == 18.0 and ds.fmt_seconds(18.0) == "18 s"
    text = ds.summary_text(recs, ["VISITID", "PSFREF", "SATSPOTS"])
    assert text.startswith("10 files (1 unreadable)")
    assert "- VISITID = V1: 6 files · PSFREF = 0 (science target)" in text
    assert "  - SATSPOTS = True (satellite spots): 2 files" in text
    assert "Varies:" in text and "Same everywhere:" in text and "CFAMNAME=1F" in text


def test_backend_keeps_only_recipe_names_of_long_recipe_cards(tmp_path):
    from astropy.io import fits
    from corgidrp_gui.backend import default_backend
    recipe = {"name": "l2b_to_l3", "template": False, "steps": [{"name": f"step_{i}", "keywords": {"x": i}} for i in range(60)]}
    hdr = fits.Header()
    hdr["DATALVL"] = "L3"
    hdr["RECIPE"] = json.dumps(recipe)
    hdr["RECIPE2"] = json.dumps({"name": "tiny"})        # short enough to stay one card
    p = tmp_path / "f.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(header=hdr)]).writeto(p)
    h = default_backend().read_headers(str(p))
    assert h.error is None and h.get("DATALVL") == "L3"
    assert h.get("RECIPE") == "l2b_to_l3"
    assert json.loads(h.get("RECIPE2"))["name"] == "tiny"
