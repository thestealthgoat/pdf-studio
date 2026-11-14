from __future__ import annotations

import pikepdf
import pypdf
import pytest

from pdfcore.cli import main
from pdfcore.ops import navigate, organize
from pdfcore.ops.navigate import Bookmark


def npages(path, password: str = "") -> int:
    with pikepdf.open(path, password=password) as pdf:
        return len(pdf.pages)


def first_line(path, page: int) -> str:
    """First text line of a page — identifies which original page ended up where."""
    reader = pypdf.PdfReader(str(path))
    return (reader.pages[page].extract_text() or "").strip().splitlines()[0]


def outline(path) -> list[tuple[int, str, int | None]]:
    return [(d, b.title, b.page) for d, b in navigate.flatten(navigate.read_bookmarks(path))]


def with_bookmarks(work, name: str):
    src = work(name)
    return navigate.auto_bookmarks(src, quiet=True).output


# ---------------------------------------------------------------- merge


def test_merge_nests_each_files_bookmarks(work):
    a = with_bookmarks(work, "report_tables")
    b = work("contract_v1")
    res = organize.merge([a, b], quiet=True)
    assert res.ok, res.line()
    assert res.output.exists() and res.output != a
    assert npages(res.output) == 3 + 4
    ol = outline(res.output)
    assert ol[0] == (0, a.stem, 0)
    assert (1, "3 Materials", 1) in ol
    assert (0, "contract_v1", 3) in ol
    assert a.exists() and npages(a) == 3  # input untouched


def test_merge_flat_and_forms(work):
    f = work("form_fillable")
    res = organize.merge([f, f], file_bookmarks=False, quiet=True)
    assert res.ok, res.line()
    fields = pypdf.PdfReader(str(res.output)).get_fields()
    assert len(fields) == 2 * len(pypdf.PdfReader(str(f)).get_fields())  # second copy's fields renamed, kept
    assert navigate.read_bookmarks(res.output) == []


def test_merge_keeps_internal_links(work):
    src = work("contract_v1")
    linked = navigate.add_link(src, 0, (72, 72, 200, 90), to_page=2, quiet=True).output
    res = organize.merge([linked, linked], quiet=True)
    assert res.ok, res.line()
    targets = sorted((l["page"], l["to_page"]) for l in navigate.list_links(res.output))
    assert targets == [(1, 3), (5, 7)]


# ---------------------------------------------------------------- split


def test_split_by_ranges(work):
    src = work("contract_v1")
    res = organize.split(src, ranges="1-2,3-4", quiet=True)
    assert res.ok, res.line()
    parts = [res.output, *res.extra_outputs]
    assert [npages(p) for p in parts] == [2, 2]
    assert first_line(parts[1], 0) == first_line(src, 2)


def test_split_every_n_into_dir(work, tmp_path):
    src = work("slides")
    outdir = tmp_path / "parts"
    outdir.mkdir()
    res = organize.split(src, outdir, every=4, quiet=True)
    assert res.ok
    parts = [res.output, *res.extra_outputs]
    assert all(p.parent == outdir for p in parts)
    assert [npages(p) for p in parts] == [4, 2]


def test_split_by_bookmarks(work):
    merged = organize.merge([work("letter"), work("contract_v1"), work("slides")], quiet=True).output
    res = organize.split(merged, bookmarks=True, quiet=True)
    assert res.ok, res.line()
    parts = [res.output, *res.extra_outputs]
    assert [npages(p) for p in parts] == [1, 4, 6]
    assert "contract-v1" in parts[1].name


def test_split_needs_one_mode(work):
    with pytest.raises(ValueError):
        organize.split(work("letter"), quiet=True)


# ---------------------------------------------------------------- insert / delete / reorder / extract / duplicate


def test_insert_at_position(work):
    a, b = work("contract_v1"), work("slides")
    res = organize.insert(a, b, at=1, pages=[0, 2], quiet=True)
    assert res.ok, res.line()
    assert npages(res.output) == 6
    assert first_line(res.output, 1) == first_line(b, 0)
    assert first_line(res.output, 2) == first_line(b, 2)
    assert first_line(res.output, 3) == first_line(a, 1)


def test_delete_pages_fixes_bookmarks_and_links(work):
    src = with_bookmarks(work, "report_tables")
    linked = navigate.add_link(src, 0, (72, 72, 200, 90), to_page=1, quiet=True).output
    linked = navigate.add_link(linked, 0, (72, 100, 200, 120), to_page=2, quiet=True).output
    res = organize.delete_pages(linked, [1], quiet=True)
    assert res.ok, res.line()
    assert npages(res.output) == 2
    titles = {t: p for _, t, p in outline(res.output)}
    assert "3 Materials" not in titles and titles["5 Outlook"] == 1
    assert [l["to_page"] for l in navigate.list_links(res.output)] == [2]  # link to the deleted page removed


def test_delete_all_refused(work):
    with pytest.raises(ValueError):
        organize.delete_pages(work("letter"), [0], quiet=True)


def test_reorder(work):
    src = work("slides")
    order = [5, 4, 3, 2, 1, 0]
    res = organize.reorder(src, order, quiet=True)
    assert res.ok, res.line()
    assert [first_line(res.output, i) for i in range(6)] == [first_line(src, i) for i in order]
    with pytest.raises(ValueError):
        organize.reorder(src, [0, 1], quiet=True)


def test_extract_pages(work):
    src = work("contract_v1")
    res = organize.extract_pages(src, [3, 0], quiet=True)
    assert res.ok, res.line()
    assert npages(res.output) == 2
    assert first_line(res.output, 0) == first_line(src, 3)


def test_duplicate_pages_with_form(work):
    src = work("form_fillable")
    res = organize.duplicate_pages(src, [0], copies=2, quiet=True)
    assert res.ok, res.line()
    assert npages(res.output) == 3
    assert len(pypdf.PdfReader(str(res.output)).get_fields()) == len(pypdf.PdfReader(str(src)).get_fields())
    with pikepdf.open(res.output) as pdf:
        objs = [{a.objgen for a in p.obj.Annots} for p in pdf.pages]
        assert not (objs[0] & objs[1])  # each copy has its own widgets


# ---------------------------------------------------------------- rotate / crop


def test_rotate_selected(work):
    src = work("contract_v1")
    res = organize.rotate(src, 90, [0, 2], quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [90, 0, 90, 0]
    res2 = organize.rotate(res.output, 270, [0], quiet=True)
    with pikepdf.open(res2.output) as pdf:
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) % 360 == 0
    with pytest.raises(ValueError):
        organize.rotate(src, 45, quiet=True)


def test_rotate_encrypted(work):
    src = work("encrypted")
    res = organize.rotate(src, 180, [1], password="test", quiet=True)
    assert res.ok, res.line()
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(res.output)  # encryption kept
    with pikepdf.open(res.output, password="test") as pdf:
        assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 180


def test_crop_margins_and_trim(work):
    src = work("letter")
    res = organize.crop(src, margins=organize.parse_margins("0.5in"), trim=True, quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        p = pdf.pages[0].obj
        assert [float(v) for v in p.CropBox] == [36, 36, 576, 756]
        assert [float(v) for v in p.TrimBox] == [36, 36, 576, 756]


def test_crop_box_on_rotated_page(work):
    src = organize.rotate(work("letter"), 90, quiet=True).output
    # visual page is 792 wide x 612 tall; keep the visual left half
    res = organize.crop(src, box=(0, 0, 396, 612), quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        # visual left = user-space bottom for /Rotate 90
        assert [float(v) for v in pdf.pages[0].obj.CropBox] == [0, 0, 612, 396]


def test_parse_lengths():
    assert organize.parse_length("0.5in") == 36
    assert organize.parse_length("25.4mm") == pytest.approx(72)
    assert organize.parse_margins("1in,0.5in") == (72, 36, 72, 36)
    with pytest.raises(ValueError):
        organize.parse_length("3 furlongs")


# ---------------------------------------------------------------- page labels


def test_page_labels_roman_then_prefixed(work):
    src = work("contract_v1")
    res = organize.page_labels(src, [organize.LabelRange(0, "roman"), organize.LabelRange(2, "decimal", "E-", 1)],
                               quiet=True)
    assert res.ok, res.line()
    assert pypdf.PdfReader(str(res.output)).page_labels == ["i", "ii", "E-1", "E-2"]


def test_page_labels_follow_reorder(work):
    lab = organize.page_labels(work("contract_v1"), [(0, "r"), (2, "D", "E-", 1)], quiet=True).output
    res = organize.delete_pages(lab, [1], quiet=True)
    assert pypdf.PdfReader(str(res.output)).page_labels == ["i", "E-1", "E-2"]


def test_label_spec():
    assert organize.parse_label_spec("5-:decimal:E-:1", 8) == organize.LabelRange(4, "D", "E-", 1)
    assert organize.parse_label_spec("1-4:roman", 8) == organize.LabelRange(0, "r", "", 1)


# ---------------------------------------------------------------- CLI


def test_cli_roundtrip(work, capsys):
    src = work("report_tables")
    assert main(["rotate", str(src), "90", "--pages", "2"]) == 0
    assert main(["page-labels", str(src), "--range", "1:roman", "--range", "2-:decimal:A-"]) == 0
    assert main(["split", str(src), "--ranges", "1,2-3"]) == 0
    assert main(["reorder", str(src), "1,2"]) == 2  # incomplete order → error
    out = capsys.readouterr().out
    assert out.count("PASS") == 4
    rotated = src.with_name("report_tables.rotated.pdf")
    with pikepdf.open(rotated) as pdf:
        assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 90


def test_set_bookmarks_survive_extract(work):
    src = work("contract_v1")
    bm = navigate.set_bookmarks(src, [Bookmark("A", 0), Bookmark("B", 2, [Bookmark("B.1", 3)])], quiet=True).output
    res = organize.extract_pages(bm, [2, 3], quiet=True)
    assert outline(res.output) == [(0, "B", 0), (1, "B.1", 1)]
