from __future__ import annotations

import json

import pikepdf
import pypdf
import pytest
import yaml

from pdfcore.cli import main
from pdfcore.ops import navigate
from pdfcore.ops.navigate import Bookmark
from pdfcore.text import search


def flat(path) -> list[tuple[int, str, int | None]]:
    return [(d, b.title, b.page) for d, b in navigate.flatten(navigate.read_bookmarks(path))]


def heading_page(src, title: str) -> int:
    return search(src, title, case=True)[0].page


# ---------------------------------------------------------------- auto bookmarks


def test_auto_bookmarks_report(work):
    src = work("report_tables")
    res = navigate.auto_bookmarks(src, quiet=True)
    assert res.ok, res.line()
    got = {t: p for _, t, p in flat(res.output)}
    for title in ["1 Summary", "2 Labor", "3 Materials", "4 Equipment", "5 Outlook"]:
        assert got[title] == heading_page(src, title), title
    assert "Item Qty Unit cost ($) Total ($)" not in got  # bold table header (smaller than body) is not a heading
    assert all(t.strip() not in {"1", "2", "3"} for t in got)  # page-number footers excluded


def test_auto_bookmarks_contract(work):
    src = work("contract_v1")
    res = navigate.auto_bookmarks(src, quiet=True)
    assert res.ok, res.line()
    items = flat(res.output)
    titles = [t for _, t, _ in items]
    assert titles[0] == "Professional Services Agreement"
    sections = [t for t in titles if t[0].isdigit()]
    assert sections[0] == "1. Scope of Work" and sections[-1] == "10. General Provisions" and len(sections) == 10
    got = {t: p for _, t, p in items}
    assert got["6. Indemnification"] == heading_page(src, "6. Indemnification")
    assert not any("Services Agreement — version" in t for t in titles)  # running footer


def test_auto_bookmarks_destination_top(work):
    src = work("report_tables")
    out = navigate.auto_bookmarks(src, quiet=True).output
    bm = next(b for b in navigate.read_bookmarks(out) if b.title == "3 Materials")
    hit = search(src, "3 Materials", case=True)[0]
    assert bm.top == pytest.approx(792 - hit.rects[0].top, abs=4)


def test_running_headers_are_dropped():
    from pdfcore.ops.navigate import Heading, _drop_running

    hs = [Heading("ACME Corp Page 1", 0, 14, True, 10, 20, 72), Heading("ACME Corp Page 2", 1, 14, True, 10, 20, 72),
          Heading("ACME Corp Page 3", 2, 14, True, 10, 20, 72), Heading("Introduction", 0, 14, True, 100, 110, 72)]
    assert [h.text for h in _drop_running(hs, 3)] == ["Introduction"]


def test_auto_bookmarks_levels(work):
    from pdfcore.ops.navigate import Heading, _assign_levels, headings_to_outline

    hs = [Heading("Doc", 0, 24, True, 0, 1, 0), Heading("Chapter", 0, 18, True, 0, 1, 0),
          Heading("Section", 1, 14, True, 0, 1, 0), Heading("Chapter 2", 2, 18, True, 0, 1, 0)]
    items = headings_to_outline(_assign_levels(hs, 3), [792, 792, 792])
    assert [b.title for b in items] == ["Doc", "Chapter", "Chapter 2"]  # lone title on p.1 not a parent
    assert items[1].children[0].title == "Section"


# ---------------------------------------------------------------- set / list bookmarks


def test_set_bookmarks_from_yaml_and_json(work, tmp_path):
    src = work("contract_v1")
    spec = [{"title": "Front", "page": 1, "children": [{"title": "Scope", "page": 1}]},
            {"title": "Back", "page": 4}]
    y = tmp_path / "outline.yaml"
    y.write_text(yaml.safe_dump(spec))
    res = navigate.set_bookmarks(src, y, quiet=True)
    assert res.ok, res.line()
    assert flat(res.output) == [(0, "Front", 0), (1, "Scope", 0), (0, "Back", 3)]

    lst = navigate.list_bookmarks(res.output, as_json=True, quiet=True)
    j = tmp_path / "outline.json"
    j.write_text(lst.data["text"])
    again = navigate.set_bookmarks(work("contract_v1"), j, quiet=True)
    assert flat(again.output) == flat(res.output)
    assert "  Scope  (p.1)" in navigate.list_bookmarks(res.output, quiet=True).data["text"]


def test_set_bookmarks_bad_page(work):
    with pytest.raises(ValueError):
        navigate.set_bookmarks(work("letter"), [Bookmark("x", 5)], quiet=True)


def test_cli_bookmarks(work, capsys):
    src = work("slides")
    assert main(["bookmarks", str(src), "--auto"]) == 0
    out = src.with_name("slides.bookmarks.pdf")
    capsys.readouterr()
    assert main(["bookmarks", str(out), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["title"] for d in data] == ["Project Kickoff", "Scope", "Schedule", "Budget", "Risks", "Next Steps"]
    assert [d["page"] for d in data] == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------- links


def test_internal_external_and_named_links(work):
    src = work("contract_v1")
    d = navigate.add_named_dest(src, "termination", 2, quiet=True)
    assert d.ok, d.line()
    l1 = navigate.add_link(d.output, 0, (72, 72, 200, 90), to_page=3, quiet=True)
    l2 = navigate.add_link(l1.output, 0, (72, 100, 200, 120), dest_name="termination", quiet=True)
    l3 = navigate.add_link(l2.output, 1, (72, 100, 200, 120), uri="https://example.com/terms", quiet=True)
    for r in (l1, l2, l3):
        assert r.ok, r.line()
    links = navigate.list_links(l3.output)
    assert {"page": 1, "rect": [72.0, 702.0, 200.0, 720.0], "to_page": 4} in links
    assert any(l.get("dest_name") == "termination" and l["to_page"] == 3 for l in links)
    assert any(l.get("uri") == "https://example.com/terms" and l["page"] == 2 for l in links)
    with pytest.raises(ValueError):
        navigate.add_link(src, 0, (0, 0, 10, 10), dest_name="nope", quiet=True)


def test_autolink_finds_emails_and_urls(work, tmp_path):
    src = work("letter")
    res = navigate.autolink(src, quiet=True)
    assert res.ok, res.line()
    uris = [l["uri"] for l in navigate.list_links(res.output)]
    assert "mailto:orders@northfield.example" in uris
    again = navigate.autolink(res.output, quiet=True)  # already linked → nothing new
    assert again.data["links"] == []

    from reportlab.pdfgen import canvas

    p = tmp_path / "urls.pdf"
    c = canvas.Canvas(str(p))
    c.setFont("Helvetica", 11)
    c.drawString(72, 700, "See https://example.org/docs. Or www.example.com, or mail bob@example.com today.")
    c.showPage()
    c.save()
    uris = sorted(l["uri"] for l in navigate.list_links(navigate.autolink(p, quiet=True).output))
    assert uris == ["https://example.org/docs", "https://www.example.com", "mailto:bob@example.com"]


def test_cli_link(work):
    src = work("report_tables")
    assert main(["link", str(src), "--page", "1", "--rect", "72,72,200,90", "--to-page", "last"]) == 0
    links = navigate.list_links(src.with_name("report_tables.linked.pdf"))
    assert links[0]["to_page"] == 3


# ---------------------------------------------------------------- metadata


def test_metadata_roundtrip(work):
    src = work("letter")
    res = navigate.set_metadata(src, title="Order Confirmation", author="Pat Lee; Sam Ng", subject="Orders",
                                keywords=["orders", "supply"], language="en-US", quiet=True)
    assert res.ok, res.line()
    m = navigate.read_metadata(res.output)
    assert m["title"] == "Order Confirmation"
    assert m["author"] == "Pat Lee; Sam Ng"
    assert m["subject"] == "Orders"
    assert m["keywords"] == "orders, supply"
    assert m["language"] == "en-US"
    with pikepdf.open(res.output) as pdf:
        assert str(pdf.docinfo.Title) == "Order Confirmation"
        with pdf.open_metadata() as x:
            assert x["dc:title"] == "Order Confirmation"
            assert list(x["dc:creator"]) == ["Pat Lee", "Sam Ng"]
    cleared = navigate.set_metadata(res.output, subject="", quiet=True)
    assert "subject" not in navigate.read_metadata(cleared.output)


def test_metadata_on_encrypted(work):
    src = work("encrypted")
    res = navigate.set_metadata(src, title="Secret report", password="test", quiet=True)
    assert res.ok, res.line()
    assert navigate.read_metadata(res.output, password="test")["title"] == "Secret report"


def test_cli_meta(work, capsys):
    src = work("letter")
    assert main(["meta", str(src), "--title", "T1", "--lang", "fr-CA"]) == 0
    capsys.readouterr()
    assert main(["meta", str(src.with_name("letter.meta.pdf"))]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["title"] == "T1" and data["language"] == "fr-CA"


# ---------------------------------------------------------------- attachments


def test_attachments_add_list_extract_remove(work, tmp_path):
    src = work("letter")
    a = tmp_path / "data.csv"
    a.write_bytes(b"id,qty\n1,40\n2,7\n")
    b = tmp_path / "photo.bin"
    b.write_bytes(bytes(range(256)) * 10)
    res = navigate.add_attachments(src, [a, b], description="backup", quiet=True)
    assert res.ok, res.line()
    rows = {r["name"]: r for r in navigate.list_attachments(res.output).data["attachments"]}
    assert rows["data.csv"]["size"] == a.stat().st_size and rows["data.csv"]["mime"] == "text/csv"
    assert rows["photo.bin"]["description"] == "backup"

    ex = navigate.extract_attachments(res.output, tmp_path / "out", quiet=True)
    got = {p.name: p.read_bytes() for p in ex.extra_outputs}
    assert got == {"data.csv": a.read_bytes(), "photo.bin": b.read_bytes()}
    ex2 = navigate.extract_attachments(res.output, tmp_path / "out", ["data.csv"], quiet=True)
    assert ex2.extra_outputs[0].name == "data-2.csv"  # never overwrites

    rm = navigate.remove_attachments(res.output, ["photo.bin"], quiet=True)
    assert rm.ok, rm.line()
    assert [r["name"] for r in navigate.list_attachments(rm.output).data["attachments"]] == ["data.csv"]
    with pytest.raises(KeyError):
        navigate.remove_attachments(rm.output, ["missing.txt"], quiet=True)


def test_cli_attach(work, tmp_path, capsys):
    src = work("letter")
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    assert main(["attach", str(src), "--add", str(f)]) == 0
    out = src.with_name("letter.attached.pdf")
    assert main(["attach", str(out), "--extract", str(tmp_path / "x")]) == 0
    assert (tmp_path / "x" / "notes.txt").read_text() == "hello"
    assert pypdf.PdfReader(str(out)).attachments.keys() == {"notes.txt"}
