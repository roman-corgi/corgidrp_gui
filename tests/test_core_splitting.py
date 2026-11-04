"""Input splitting (max files per recipe, by header keyword)."""
from types import SimpleNamespace

from corgidrp_gui.core.splitting import Batch, SplitSpec, batch_recipe_name, split_items, split_paths


def _item(name, **hdr):
    return SimpleNamespace(name=name, hdr=hdr)


def _value_of(it, k):
    return it.hdr.get(k)


def test_spec_parsing_and_description():
    assert SplitSpec.parse_keywords("visitid, DPAMNAME ;exptime visitid") == ["VISITID", "DPAMNAME", "EXPTIME"]
    assert not SplitSpec().active and SplitSpec().describe() == "no splitting"
    s = SplitSpec(max_files=5, keywords=["VISITID"])
    assert s.active and "VISITID" in s.describe() and "5 files" in s.describe()
    assert s.keywords_text == "VISITID"


def test_no_split_is_one_batch():
    items = [_item(i) for i in range(4)]
    b = split_items(items, SplitSpec(), _value_of)
    assert len(b) == 1 and b[0].items == items and b[0].label == "" and b[0].slug == ""
    assert batch_recipe_name("l1_to_l2b", b[0]) == "l1_to_l2b"


def test_max_files_chunks_in_order():
    items = [_item(i) for i in range(7)]
    b = split_items(items, SplitSpec(max_files=3), _value_of)
    assert [len(x.items) for x in b] == [3, 3, 1]
    assert [x.label for x in b] == ["part 1/3", "part 2/3", "part 3/3"]
    assert b[1].items == items[3:6]
    assert batch_recipe_name("l1_to_l2b", b[2]) == "l1_to_l2b_part3of3"
    # exactly at the limit: no split
    assert len(split_items(items[:3], SplitSpec(max_files=3), _value_of)) == 1


def test_keyword_split_sorted_and_then_chunked():
    items = [_item(0, VISITID=2, DPAMNAME="POL45"), _item(1, VISITID=1, DPAMNAME="POL0"),
             _item(2, VISITID=2, DPAMNAME="POL0"), _item(3, VISITID=1, DPAMNAME="POL0"),
             _item(4, DPAMNAME="POL0")]
    b = split_items(items, SplitSpec(keywords=["VISITID", "DPAMNAME"]), _value_of)
    assert [x.label for x in b] == ["VISITID=1 · DPAMNAME=POL0", "VISITID=2 · DPAMNAME=POL0",
                                    "VISITID=2 · DPAMNAME=POL45", "VISITID=? · DPAMNAME=POL0"]
    assert [i.name for i in b[0].items] == [1, 3]
    assert b[0].slug == "VISITID-1_DPAMNAME-POL0" and b[3].slug == "VISITID-unknown_DPAMNAME-POL0"
    b = split_items(items, SplitSpec(max_files=1, keywords=["VISITID"]), _value_of)
    assert [x.label for x in b] == ["VISITID=1 · part 1/2", "VISITID=1 · part 2/2",
                                    "VISITID=2 · part 1/2", "VISITID=2 · part 2/2", "VISITID=?"]


def test_split_paths_reads_headers_once_per_file():
    reads = []

    def headers_of(p):
        reads.append(p)
        return SimpleNamespace(get=lambda k, d=None: {"a.fits": "V1", "b.fits": "V2", "c.fits": "V1"}[p] if k == "VISITID" else d)

    b = split_paths(["a.fits", "b.fits", "c.fits"], SplitSpec(keywords=["VISITID"]), headers_of)
    assert [x.items for x in b] == [["a.fits", "c.fits"], ["b.fits"]] and len(reads) == 3
    assert split_paths(["a.fits"], SplitSpec(max_files=2))[0].items == ["a.fits"]
    try:
        split_paths(["a.fits"], SplitSpec(keywords=["VISITID"]))
    except ValueError as exc:
        assert "headers_of" in str(exc)
    else:
        raise AssertionError("expected ValueError")
