#!/usr/bin/env python3
"""Generate fixtures/bar/: invented QuickBooks-Online-style estimate look-alikes + expected pdfcore-words/1 JSON.

All names, addresses, numbers and items are invented (contract §4: no real BAR data).
Run:  python tools/make_bar_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfbase.pdfmetrics import stringWidth  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from pdfcore.fonts import register_reportlab  # noqa: E402
from pdfcore.words import dumps, words_document  # noqa: E402

OUT = ROOT / "fixtures" / "bar"
GREEN = colors.HexColor("#2CA01C")  # the familiar "online accounting" green, used generically
GREY = colors.HexColor("#6B6C72")

COMPANY = {"name": "Riverbend Process Controls LLC", "lines": ["1180 Levee Road", "Port Allen, LA 70767", "+1 (225) 555-0148",
                                                               "estimates@riverbend-controls.example"]}
CUSTOMER = {"bill": ["Delta Water District", "Attn: Purchasing", "400 Canal St", "Plaquemine, LA 70764"],
            "ship": ["Delta Water District", "Lift Station 7", "2210 Bayou Rd", "Plaquemine, LA 70764"]}


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    out, line = [], ""
    for word in text.split():
        t = (line + " " + word).strip()
        if stringWidth(t, font, size) > width and line:
            out.append(line)
            line = word
        else:
            line = t
    if line:
        out.append(line)
    return out


def estimate(path: Path, *, number: str, date: str, expires: str, items: list[tuple[str, str, float, float]],
             fonts: str = "embedded", note: str = "", tax_rate: float = 0.0) -> None:
    F = register_reportlab()
    if fonts == "embedded":
        R, B = F["sans"], F["sans-bold"]
    else:  # standard-14 Helvetica, not embedded — exercises readers that rely on built-in metrics
        R, B = "Helvetica", "Helvetica-Bold"
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)  # no timestamps or random IDs: reproducible bytes
    c.setTitle(f"Estimate {number}")
    c.setAuthor(COMPANY["name"])
    W, H = letter
    page = [1]

    def header(first: bool) -> float:
        y = H - 0.6 * inch
        c.setFillColor(GREEN)
        c.rect(0.6 * inch, y - 0.55 * inch, 0.55 * inch, 0.55 * inch, stroke=0, fill=1)  # logo block
        c.setFillColor(colors.white)
        c.setFont(B, 16)
        c.drawCentredString(0.875 * inch, y - 0.37 * inch, "RB")
        c.setFillColor(colors.black)
        c.setFont(B, 11)
        c.drawString(1.3 * inch, y - 0.15 * inch, COMPANY["name"])
        c.setFont(R, 9)
        for k, ln in enumerate(COMPANY["lines"]):
            c.drawString(1.3 * inch, y - (0.32 + 0.14 * k) * inch, ln)
        c.setFont(B, 22)
        c.setFillColor(GREEN)
        c.drawRightString(W - 0.6 * inch, y - 0.3 * inch, "Estimate")
        c.setFillColor(colors.black)
        y -= 1.2 * inch
        if not first:
            c.setFont(R, 9)
            c.drawString(0.6 * inch, y, f"Estimate {number} (continued)")
            return y - 0.35 * inch
        c.setFont(B, 8)
        c.setFillColor(GREY)
        c.drawString(0.6 * inch, y, "BILL TO")
        c.drawString(2.7 * inch, y, "SHIP TO")
        c.drawString(5.2 * inch, y, "ESTIMATE #")
        c.drawString(5.2 * inch, y - 0.3 * inch, "DATE")
        c.drawString(5.2 * inch, y - 0.6 * inch, "EXPIRATION DATE")
        c.setFillColor(colors.black)
        c.setFont(R, 9)
        for k, ln in enumerate(CUSTOMER["bill"]):
            c.drawString(0.6 * inch, y - (0.16 + 0.14 * k) * inch, ln)
        for k, ln in enumerate(CUSTOMER["ship"]):
            c.drawString(2.7 * inch, y - (0.16 + 0.14 * k) * inch, ln)
        c.drawRightString(W - 0.6 * inch, y, number)
        c.drawRightString(W - 0.6 * inch, y - 0.3 * inch, date)
        c.drawRightString(W - 0.6 * inch, y - 0.6 * inch, expires)
        return y - 1.05 * inch

    cols = [0.6 * inch, 2.1 * inch, 5.35 * inch, 6.15 * inch, W - 0.6 * inch]  # Activity | Description | Qty | Rate | Amount

    def table_head(y: float) -> float:
        c.setFillColor(colors.HexColor("#EAF6E8"))
        c.rect(0.6 * inch, y - 0.06 * inch, W - 1.2 * inch, 0.24 * inch, stroke=0, fill=1)
        c.setFillColor(GREEN)
        c.setFont(B, 8)
        c.drawString(cols[0] + 4, y, "ACTIVITY")
        c.drawString(cols[1], y, "DESCRIPTION")
        c.drawRightString(cols[2] + 0.35 * inch, y, "QTY")
        c.drawRightString(cols[3] + 0.6 * inch, y, "RATE")
        c.drawRightString(cols[4] - 4, y, "AMOUNT")
        c.setFillColor(colors.black)
        return y - 0.3 * inch

    def footer():
        c.setFont(R, 8)
        c.setFillColor(GREY)
        c.drawRightString(W - 0.6 * inch, 0.45 * inch, f"Page {page[0]}")
        c.setFillColor(colors.black)

    y = table_head(header(True))
    subtotal = 0.0
    for activity, desc, qty, rate in items:
        lines = wrap(desc, R, 9, cols[2] - cols[1] - 0.25 * inch)
        need = 0.17 * inch * len(lines) + 0.12 * inch
        if y - need < 1.6 * inch:
            footer()
            c.showPage()
            page[0] += 1
            y = table_head(header(False))
        amount = round(qty * rate, 2)
        subtotal += amount
        c.setFont(B, 9)
        c.drawString(cols[0] + 4, y, activity)
        c.setFont(R, 9)
        for k, ln in enumerate(lines):
            c.drawString(cols[1], y - k * 0.17 * inch, ln)
        c.drawRightString(cols[2] + 0.35 * inch, y, f"{qty:g}")
        c.drawRightString(cols[3] + 0.6 * inch, y, f"{rate:,.2f}")
        c.drawRightString(cols[4] - 4, y, f"{amount:,.2f}")
        y -= need
        c.setStrokeColor(colors.HexColor("#DDDDDD"))
        c.line(0.6 * inch, y + 0.18 * inch, W - 0.6 * inch, y + 0.18 * inch)
    tax = round(subtotal * tax_rate, 2)
    y -= 0.2 * inch
    c.setFont(R, 9)
    c.drawRightString(6.15 * inch, y, "SUBTOTAL")
    c.drawRightString(W - 0.6 * inch - 4, y, f"{subtotal:,.2f}")
    if tax_rate:
        y -= 0.2 * inch
        c.drawRightString(6.15 * inch, y, f"TAX ({tax_rate * 100:g}%)")
        c.drawRightString(W - 0.6 * inch - 4, y, f"{tax:,.2f}")
    y -= 0.3 * inch
    c.setFont(B, 11)
    c.drawRightString(6.15 * inch, y, "TOTAL")
    c.drawRightString(W - 0.6 * inch - 4, y, f"${subtotal + tax:,.2f}")
    if note:
        c.setFont(R, 8.5)
        for k, ln in enumerate(wrap(note, R, 8.5, 4.2 * inch)):
            c.drawString(0.6 * inch, y - k * 0.15 * inch, ln)
    y -= 0.9 * inch
    c.setFont(R, 9)
    c.drawString(0.6 * inch, y, "Accepted By")
    c.line(1.45 * inch, y - 2, 3.6 * inch, y - 2)
    c.drawString(4.0 * inch, y, "Accepted Date")
    c.line(5.05 * inch, y - 2, W - 0.6 * inch, y - 2)
    footer()
    c.showPage()
    c.save()


ITEMS_SHORT = [
    ("Control Panel", "UL 508A pump control panel, NEMA 4X stainless, duplex alternation", 1, 14850.00),
    ("Labor", "Field installation and terminations", 24, 95.00),
    ("Startup", "Startup and commissioning, one trip", 1, 1200.00),
]
ITEMS_MULTI = [
    ("Control Panel", "UL 508A duplex pump control panel, 36x30x12 NEMA 4X 304 stainless enclosure, main disconnect, "
                      "two 10 HP soft starters, PLC with 7 in HMI, float and transducer inputs, alarm light and horn", 1, 18420.00),
    ("SCADA", "Cellular RTU, antenna, programming to district SCADA, alarm call-out testing", 1, 4310.50),
    ("Labor", "Electricians, install panel on new rack, pull and terminate wire from wet well, 3 days", 48, 92.00),
    ("Materials", "Conduit, fittings, wire, rack hardware", 1, 1875.25),
    ("Discount", "Repeat-customer discount", 1, -500.00),
]
ITEMS_LONG = ITEMS_MULTI + [
    (f"Item {k}", f"Spare part {k}: relay, fuse kit, terminal block assortment and labels for panel maintenance", k % 3 + 1, 37.5 * k)
    for k in range(1, 25)
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("qbo-lookalike-1page", dict(number="1047", date="11/02/2026", expires="12/02/2026", items=ITEMS_SHORT)),
        ("qbo-lookalike-multiline", dict(number="1048", date="11/05/2026", expires="12/05/2026", items=ITEMS_MULTI,
                                         tax_rate=0.0945, note="Pricing valid 30 days. Excludes permits, utility fees and "
                                         "concrete work. Lead time 6-8 weeks after approved submittals.")),
        ("qbo-lookalike-multipage", dict(number="1049", date="11/09/2026", expires="12/09/2026", items=ITEMS_LONG, tax_rate=0.0945)),
        ("qbo-lookalike-helvetica", dict(number="1050", date="11/11/2026", expires="12/11/2026", items=ITEMS_SHORT,
                                         fonts="standard")),
    ]
    for name, kw in jobs:
        pdf = OUT / f"{name}.pdf"
        estimate(pdf, **kw)
        doc = words_document(pdf, version="0.0.0")  # fixed producer version → expected files stay stable
        (OUT / f"{name}.words.json").write_text(dumps(doc), encoding="utf-8")
        print(f"{pdf.name}: {doc['source']['pageCount']} page(s), {len(doc['words'])} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
