#!/usr/bin/env python3
"""Generate the self-made test corpus into corpus/generated/ (all content original, CC0).

Run:  python corpus/build.py            (idempotent; skips files that exist unless --force)
"""

from __future__ import annotations

import argparse
import io
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "generated"
sys.path.insert(0, str(ROOT.parent))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import letter, landscape  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from pdfcore.fonts import register_reportlab  # noqa: E402

F = register_reportlab()
SANS, SANS_B, SERIF, SERIF_B = F["sans"], F["sans-bold"], F["serif"], F["serif-bold"]

LOREM = (
    "The contractor shall furnish all labor, materials, equipment and supervision required to complete the work "
    "described in these documents. Work shall conform to the applicable codes and to the drawings and specifications. "
    "Any conflict between documents shall be brought to the attention of the owner's representative in writing before "
    "work proceeds. Payment terms are net thirty days from receipt of a correct invoice."
).split()


def para(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(LOREM) for _ in range(n)).capitalize() + "."


def wrap(c: canvas.Canvas, text: str, x: float, y: float, w: float, font: str, size: float, lead: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    line = ""
    for word in text.split():
        t = (line + " " + word).strip()
        if stringWidth(t, font, size) > w:
            c.drawString(x, y, line)
            y -= lead
            line = word
        else:
            line = t
    if line:
        c.drawString(x, y, line)
        y -= lead
    return y


def meta(c: canvas.Canvas, title: str) -> None:
    c.setTitle(title)
    c.setAuthor("PDF Studio corpus")
    c.setSubject("Generated test document")
    c.setCreator("pdf-studio corpus/build.py")


# ------------------------------------------------------------------ letter + scan
def letter_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, "Sample letter")
    rng = random.Random(1)
    c.setFont(SANS_B, 16)
    c.drawString(1 * inch, 10 * inch, "Northfield Electrical Supply")
    c.setFont(SANS, 10)
    c.drawString(1 * inch, 9.78 * inch, "400 Industrial Way · Springfield · (555) 201-8890 · orders@northfield.example")
    c.setFont(SERIF, 11)
    y = 9.1 * inch
    for ln in ["November 3, 2026", "", "Ms. Dana Ruiz", "Ruiz Mechanical", "18 Harbor Street", "Springfield", "", "Dear Ms. Ruiz,"]:
        c.drawString(1 * inch, y, ln)
        y -= 15
    y -= 6
    for _ in range(4):
        y = wrap(c, para(rng, 55), 1 * inch, y, 6.5 * inch, SERIF, 11, 15) - 9
    for ln in ["Sincerely,", "", "", "Morgan Lee", "Account Manager"]:
        c.drawString(1 * inch, y, ln)
        y -= 15
    c.showPage()
    c.save()


def scanned(src: Path, dst: Path, angle: float, rotate90: bool = False, dpi: int = 200) -> None:
    import numpy as np
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    doc = pdfium.PdfDocument(str(src))
    pages = []
    rng = np.random.default_rng(7)
    for i in range(len(doc)):
        img = doc[i].render(scale=dpi / 72).to_pil().convert("L")
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
        arr = np.asarray(img).astype(np.float32)
        arr = arr * 0.92 + 12 + rng.normal(0, 6, arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
        if rotate90:
            img = img.rotate(90, expand=True)
        pages.append(img)
    doc.close()
    pages[0].save(dst, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])


# ------------------------------------------------------------------ forms
def fillable_form(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, "Vendor Registration Form")
    f = c.acroForm
    c.setFont(SANS_B, 14)
    c.drawString(0.75 * inch, 10.2 * inch, "Form VR-1   Vendor Registration and Tax Certification")
    c.setFont(SANS, 9)
    c.drawString(0.75 * inch, 10.0 * inch, "Department of Public Works — sample form for testing (not an official form)")
    c.setLineWidth(1.2)
    c.line(0.75 * inch, 9.9 * inch, 7.75 * inch, 9.9 * inch)
    y = 9.45 * inch
    fields = [("legal_name", "1  Legal name"), ("business_name", "2  Business name, if different"),
              ("address", "3  Street address"), ("city_state_zip", "4  City, state, ZIP"),
              ("tin", "5  Taxpayer identification number"), ("phone", "6  Phone"), ("email", "7  Email")]
    for name, label in fields:
        c.setFont(SANS, 9)
        c.drawString(0.75 * inch, y + 0.22 * inch, label)
        f.textfield(name=name, tooltip=label, x=0.75 * inch, y=y - 0.05 * inch, width=6.5 * inch, height=0.26 * inch,
                    fontName="Helvetica", fontSize=10, borderWidth=0.8, borderColor=colors.black, fillColor=colors.white)
        y -= 0.62 * inch
    c.setFont(SANS, 9)
    c.drawString(0.75 * inch, y + 0.2 * inch, "8  Entity type")
    for i, (val, lab) in enumerate([("sole", "Sole proprietor"), ("llc", "LLC"), ("corp", "Corporation"), ("other", "Other")]):
        x = 0.75 * inch + i * 1.6 * inch
        f.radio(name="entity", value=val, selected=(i == 0), x=x, y=y - 0.1 * inch, size=12, buttonStyle="circle", borderWidth=0.8)
        c.drawString(x + 16, y - 0.06 * inch, lab)
    y -= 0.6 * inch
    f.checkbox(name="certify", x=0.75 * inch, y=y, size=12, borderWidth=0.8, buttonStyle="check")
    c.drawString(0.75 * inch + 18, y + 2, "9  I certify the information above is correct.")
    y -= 0.5 * inch
    c.drawString(0.75 * inch, y + 0.2 * inch, "10  State of incorporation")
    f.choice(name="state", value="TX", options=["TX", "OK", "LA", "NM", "AR"], x=0.75 * inch, y=y - 0.1 * inch,
             width=1.5 * inch, height=0.26 * inch, fontName="Helvetica", fontSize=10)
    y -= 0.7 * inch
    c.drawString(0.75 * inch, y + 0.2 * inch, "Signature")
    c.line(0.75 * inch, y, 4.5 * inch, y)
    c.drawString(5 * inch, y + 0.2 * inch, "Date")
    c.line(5 * inch, y, 7.75 * inch, y)
    c.showPage()
    c.save()


def flat_form(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, "Site Visit Report (blank)")
    c.setFont(SANS_B, 14)
    c.drawString(0.75 * inch, 10.2 * inch, "Site Visit Report")
    c.setFont(SANS, 10)
    y = 9.6 * inch
    for label in ["Project name", "Project number", "Visited by", "Date of visit", "Weather"]:
        c.drawString(0.75 * inch, y, label + ":")
        c.line(2.2 * inch, y - 2, 7.75 * inch, y - 2)
        y -= 0.45 * inch
    c.drawString(0.75 * inch, y, "Work observed:")
    c.rect(0.75 * inch, y - 2.3 * inch, 7 * inch, 2.1 * inch)
    y -= 2.7 * inch
    for label in ["Safety concerns noted", "Photos taken", "Follow-up required"]:
        c.rect(0.75 * inch, y - 2, 10, 10)
        c.drawString(0.75 * inch + 16, y, label)
        y -= 0.35 * inch
    c.showPage()
    c.save()


# ------------------------------------------------------------------ contract (2 versions)
PII = [("Phone", "(555) 734-2201"), ("Email", "jordan.blake@example.com"), ("SSN", "123-45-6789"),
       ("Alt phone", "555.310.9944"), ("Tax ID", "98-7654321")]


def contract(path: Path, version: int) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, f"Services Agreement v{version}")
    rng = random.Random(42)
    sections = ["Scope of Work", "Schedule", "Compensation", "Changes", "Insurance", "Indemnification",
                "Termination", "Warranty", "Dispute Resolution", "General Provisions"]
    page = 1

    def footer():
        c.setFont(SANS, 8)
        c.drawString(1 * inch, 0.5 * inch, f"Services Agreement — version {version}")
        c.drawRightString(7.5 * inch, 0.5 * inch, f"Page {page}")

    c.setFont(SERIF_B, 18)
    c.drawString(1 * inch, 10 * inch, "Professional Services Agreement")
    c.setFont(SERIF, 11)
    y = 9.5 * inch
    y = wrap(c, "This agreement is made between Ridgeline Controls LLC (\"Contractor\") and Jordan Blake "
             f"(\"Client\"), reachable at {PII[0][1]} or {PII[1][1]}, SSN {PII[2][1]}.", 1 * inch, y, 6.5 * inch, SERIF, 11, 15) - 10
    for si, title in enumerate(sections):
        if y < 2 * inch:
            footer()
            c.showPage()
            page += 1
            y = 10 * inch
        c.setFont(SERIF_B, 13)
        c.drawString(1 * inch, y, f"{si + 1}. {title}")
        y -= 20
        c.setFont(SERIF, 11)
        n_par = 3 if si % 2 == 0 else 2
        for pj in range(n_par):
            txt = para(rng, 60)
            if version == 2 and si == 2 and pj == 0:
                txt = "Compensation is increased to $48,500 payable in three installments. " + txt
            if version == 2 and si == 6 and pj == 1:
                continue  # paragraph deleted in v2
            if si == 4 and pj == 0:
                txt += f" Questions: call {PII[3][1]}. Contractor tax ID {PII[4][1]}."
            y = wrap(c, txt, 1 * inch, y, 6.5 * inch, SERIF, 11, 15) - 9
            if y < 1.3 * inch:
                footer()
                c.showPage()
                page += 1
                y = 10 * inch
                c.setFont(SERIF, 11)
    y -= 20
    c.setFont(SERIF, 11)
    for who in ["Contractor", "Client"]:
        c.line(1 * inch, y, 4 * inch, y)
        c.drawString(1 * inch, y - 13, f"{who} signature")
        c.line(4.5 * inch, y, 7.5 * inch, y)
        c.drawString(4.5 * inch, y - 13, "Date")
        y -= 50
    footer()
    c.showPage()
    c.save()


# ------------------------------------------------------------------ report with tables
def report(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, "Quarterly Operations Report")
    rng = random.Random(3)
    page = [1]

    def new_page():
        c.setFont(SANS, 8)
        c.drawRightString(7.5 * inch, 0.5 * inch, f"{page[0]}")
        c.showPage()
        page[0] += 1

    def heading(txt, y, size=16):
        c.setFont(SANS_B, size)
        c.drawString(1 * inch, y, txt)
        return y - size - 8

    def table(y, rows, widths):
        x0 = 1 * inch
        c.setFont(SANS_B, 9)
        x = x0
        for j, h in enumerate(rows[0]):
            (c.drawRightString(x + widths[j] - 4, y, h) if j else c.drawString(x + 2, y, h))
            x += widths[j]
        c.setLineWidth(0.8)
        c.line(x0, y - 4, x0 + sum(widths), y - 4)
        y -= 16
        c.setFont(SANS, 9)
        for r in rows[1:]:
            x = x0
            for j, v in enumerate(r):
                (c.drawRightString(x + widths[j] - 4, y, v) if j else c.drawString(x + 2, y, v))
                x += widths[j]
            c.setLineWidth(0.3)
            c.line(x0, y - 4, x0 + sum(widths), y - 4)
            y -= 15
        return y - 10

    y = heading("Quarterly Operations Report", 10 * inch, 22)
    c.setFont(SERIF, 11)
    y = wrap(c, para(rng, 70), 1 * inch, y, 6.5 * inch, SERIF, 11, 15) - 10
    for sec in ["1 Summary", "2 Labor", "3 Materials", "4 Equipment", "5 Outlook"]:
        if y < 4 * inch:
            new_page()
            y = 10 * inch
        y = heading(sec, y)
        c.setFont(SERIF, 11)
        y = wrap(c, para(rng, 50), 1 * inch, y, 6.5 * inch, SERIF, 11, 15) - 8
        rows = [["Item", "Qty", "Unit cost ($)", "Total ($)"]]
        for k in range(rng.randint(5, 9)):
            q, u = rng.randint(1, 400), rng.uniform(2, 900)
            rows.append([f"{sec.split()[1]} line {k + 1}", f"{q}", f"{u:,.2f}", f"{q * u:,.2f}"])
        y = table(y, rows, [2.8 * inch, 0.9 * inch, 1.3 * inch, 1.5 * inch])
    new_page()
    c.save()


def slides(path: Path) -> None:
    W, H = 13.333 * inch, 7.5 * inch
    c = canvas.Canvas(str(path), pagesize=(W, H))
    meta(c, "Project Kickoff Deck")
    for i, t in enumerate(["Project Kickoff", "Scope", "Schedule", "Budget", "Risks", "Next Steps"]):
        c.setFillColor(colors.HexColor("#0E2A47") if i == 0 else colors.white)
        c.rect(0, 0, W, H, stroke=0, fill=1)
        c.setFillColor(colors.white if i == 0 else colors.HexColor("#0E2A47"))
        c.setFont(SANS_B, 40 if i == 0 else 30)
        c.drawString(0.8 * inch, H - 1.4 * inch, t)
        if i:
            c.setFont(SANS, 18)
            for k in range(4):
                c.drawString(1.0 * inch, H - (2.4 + 0.6 * k) * inch, f"•  Point {k + 1} about {t.lower()}")
            c.setFillColor(colors.HexColor("#3D7BB6"))
            c.rect(8.2 * inch, 1.2 * inch, 4.2 * inch, 4.2 * inch * (0.4 + 0.1 * i), stroke=0, fill=1)
        c.showPage()
    c.save()


# ------------------------------------------------------------------ security / damaged / layers / images
def encrypted(src: Path, dst: Path) -> None:
    import pikepdf

    with pikepdf.open(src) as pdf:
        pdf.save(dst, encryption=pikepdf.Encryption(user="test", owner="owner-test", R=6))


def broken(src: Path, dst: Path) -> None:
    data = bytearray(src.read_bytes())
    # Corrupt the xref offsets and drop the tail.
    i = data.rfind(b"startxref")
    data[i + 10 : i + 16] = b"999999"
    j = data.find(b"xref")
    if j > 0:
        data[j + 20 : j + 60] = b"0" * 40
    dst.write_bytes(bytes(data[:-40]))


def layers(path: Path) -> None:
    import pikepdf
    from pikepdf import Array, Dictionary, Name, String

    pdf = pikepdf.new()
    W, H = 612, 792
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    ocgs = [pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String(n))) for n in ("Electrical", "Mechanical", "Notes")]
    content = b"""
q 0 0 0 RG 2 w 72 72 468 648 re S Q
/OC /oc0 BDC q 0.85 0.1 0.1 RG 3 w 100 600 m 500 600 l S Q BT /F1 14 Tf 100 610 Td (ELECTRICAL LAYER) Tj ET EMC
/OC /oc1 BDC q 0.1 0.3 0.85 RG 3 w 100 400 m 500 400 l S Q BT /F1 14 Tf 100 410 Td (MECHANICAL LAYER) Tj ET EMC
/OC /oc2 BDC BT /F1 12 Tf 100 200 Td (General notes layer) Tj ET EMC
"""
    page = Dictionary(
        Type=Name.Page, MediaBox=Array([0, 0, W, H]),
        Resources=Dictionary(Font=Dictionary(F1=font), Properties=Dictionary(oc0=ocgs[0], oc1=ocgs[1], oc2=ocgs[2])),
        Contents=pdf.make_stream(content),
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.Root.OCProperties = Dictionary(OCGs=Array(ocgs), D=Dictionary(Order=Array(ocgs), ON=Array(ocgs), OFF=Array([])))
    pdf.docinfo["/Title"] = "Layered drawing"
    pdf.save(path)


def image_heavy(path: Path, pages: int = 4) -> None:
    import numpy as np
    from PIL import Image

    c = canvas.Canvas(str(path), pagesize=letter)
    meta(c, "Image-heavy brochure")
    rng = np.random.default_rng(11)
    for i in range(pages):
        w, h = 2250, 1650  # 7.5 x 5.5 in at 300 dpi
        yy, xx = np.mgrid[0:h, 0:w]
        base = np.stack([
            (np.sin(xx / (90 + 9 * i)) * 60 + 120 + yy * 0.03),
            (np.cos(yy / (70 + 5 * i)) * 70 + 110),
            (np.sin((xx + yy) / 140) * 50 + 140),
        ], axis=-1)
        base += rng.normal(0, 18, base.shape)
        img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
        buf = io.BytesIO()
        img.save(buf, "PNG")  # lossless: large file → good optimize test
        buf.seek(0)
        from reportlab.lib.utils import ImageReader

        c.drawImage(ImageReader(buf), 0.5 * inch, 4.5 * inch, 7.5 * inch, 5.5 * inch)
        c.setFont(SANS_B, 18)
        c.drawString(0.5 * inch, 3.8 * inch, f"Gallery page {i + 1}")
        c.setFont(SERIF, 11)
        wrap(c, para(random.Random(i), 50), 0.5 * inch, 3.4 * inch, 7.5 * inch, SERIF, 11, 15)
        c.showPage()
    c.save()


# ------------------------------------------------------------------ drawing set (ARCH D, two revisions)
ARCH_D = (36 * inch, 24 * inch)
SHEETS = [
    ("E-001", "GENERAL NOTES AND SYMBOLS"),
    ("E-101", "ELECTRICAL ONE-LINE DIAGRAM"),
    ("E-201", "PANEL SCHEDULES"),
    ("E-301", "POWER PLAN - LEVEL 1"),
    ("E-501", "ELECTRICAL DETAILS"),
]


def title_block(c, sheet_no, title, rev, date, project="RIVERSIDE PUMP STATION UPGRADE"):
    W, H = ARCH_D
    c.setLineWidth(2)
    c.rect(0.5 * inch, 0.5 * inch, W - 1 * inch, H - 1 * inch)
    tb_x = W - 4.5 * inch
    c.setLineWidth(1.5)
    c.line(tb_x, 0.5 * inch, tb_x, H - 0.5 * inch)
    c.setFont(SANS_B, 14)
    c.drawString(tb_x + 0.2 * inch, H - 1.2 * inch, "HALVERSON ENGINEERING")
    c.setFont(SANS, 9)
    c.drawString(tb_x + 0.2 * inch, H - 1.45 * inch, "Consulting Electrical Engineers")
    c.setFont(SANS, 8)
    c.drawString(tb_x + 0.2 * inch, 7 * inch, "PROJECT")
    c.setFont(SANS_B, 11)
    c.drawString(tb_x + 0.2 * inch, 6.75 * inch, project)
    # revision table
    c.setFont(SANS_B, 8)
    c.drawString(tb_x + 0.2 * inch, 5.6 * inch, "REV   DATE         DESCRIPTION")
    c.setFont(SANS, 8)
    revs = [("0", "2026-09-15", "ISSUED FOR BID")] + ([("1", "2026-10-20", "ADDENDUM 1")] if int(rev) >= 1 else [])
    for k, (r, d, desc) in enumerate(revs):
        c.drawString(tb_x + 0.2 * inch, 5.35 * inch - k * 14, f"{r:<5} {d}   {desc}")
    c.setFont(SANS, 8)
    c.drawString(tb_x + 0.2 * inch, 3.3 * inch, "SHEET TITLE")
    c.setFont(SANS_B, 12)
    c.drawString(tb_x + 0.2 * inch, 3.05 * inch, title)
    c.setFont(SANS, 8)
    c.drawString(tb_x + 0.2 * inch, 2.4 * inch, "DATE")
    c.drawString(tb_x + 2.3 * inch, 2.4 * inch, "REV")
    c.setFont(SANS_B, 11)
    c.drawString(tb_x + 0.2 * inch, 2.15 * inch, date)
    c.drawString(tb_x + 2.3 * inch, 2.15 * inch, str(rev))
    c.setFont(SANS, 8)
    c.drawString(tb_x + 0.2 * inch, 1.6 * inch, "SHEET NO.")
    c.setFont(SANS_B, 30)
    c.drawString(tb_x + 0.2 * inch, 0.9 * inch, sheet_no)


def drawing_set(path: Path, rev: int) -> None:
    W, H = ARCH_D
    c = canvas.Canvas(str(path), pagesize=ARCH_D)
    meta(c, f"Riverside Pump Station — Electrical (Rev {rev})")
    date = "2026-10-20" if rev else "2026-09-15"
    for sheet_no, title in SHEETS:
        title_block(c, sheet_no, title, rev, date)
        c.setStrokeColor(colors.black)
        if sheet_no == "E-001":
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 1.5 * inch, "GENERAL NOTES")
            c.setFont(SANS, 11)
            notes = [
                "ALL WORK SHALL COMPLY WITH NFPA 70 (NEC) 2023 EDITION AND LOCAL AMENDMENTS.",
                "CONTROL PANELS SHALL BE UL 508A LISTED AND LABELED.",
                "ALL CONDUCTORS SHALL BE COPPER, THHN/THWN-2, 600V UNLESS NOTED OTHERWISE.",
                "SEE E-201 FOR PANEL SCHEDULES.",
                "PROVIDE ARC FLASH LABELS PER NFPA 70E. SEE DETAIL 3/E-501.",
                "VFD-1 AND VFD-2 SHALL BE 480V, 3PH, 60HZ WITH LINE REACTORS.",
            ]
            if rev:
                notes.append("ADD SURGE PROTECTIVE DEVICE AT MDP. SEE E-101.")
            for k, n in enumerate(notes):
                c.drawString(1.2 * inch, H - (2.1 + 0.35 * k) * inch, f"{k + 1}.  {n}")
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 5.6 * inch, "EQUIPMENT LIST")
            c.setFont(SANS_B, 10)
            hdr = ["TAG", "DESCRIPTION", "HP", "VOLTS", "FLA"]
            xs = [1.2, 2.4, 6.4, 7.2, 8.2]
            for x, h in zip(xs, hdr):
                c.drawString(x * inch, H - 6.1 * inch, h)
            c.setFont(SANS, 10)
            eq = [("P-1", "RAW WATER PUMP 1", "75", "480", "96"), ("P-2", "RAW WATER PUMP 2", "75", "480", "96"),
                  ("EF-1", "EXHAUST FAN", "2", "480", "3.4"), ("CP-1", "PUMP CONTROL PANEL", "-", "120", "15")]
            if rev:
                eq[2] = ("EF-1", "EXHAUST FAN", "3", "480", "4.8")
            for k, row in enumerate(eq):
                for x, v in zip(xs, row):
                    c.drawString(x * inch, H - (6.45 + 0.3 * k) * inch, v)
        elif sheet_no == "E-101":
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 1.5 * inch, "ONE-LINE DIAGRAM")
            c.setLineWidth(2.5)
            cx = 14 * inch
            c.line(cx, H - 2.5 * inch, cx, H - 6 * inch)
            c.circle(cx, H - 3 * inch, 0.45 * inch)
            c.setFont(SANS, 10)
            c.drawString(cx + 0.6 * inch, H - 3 * inch, "UTILITY XFMR 1500 KVA 12.47KV-480Y/277V")
            c.setLineWidth(4)
            c.line(6 * inch, H - 6 * inch, 22 * inch, H - 6 * inch)
            c.drawString(22.3 * inch, H - 6 * inch, "MDP 2000A 480Y/277V 3PH 4W")
            c.setLineWidth(1.8)
            for k, (lab, x) in enumerate([("VFD-1 / P-1 75HP", 8), ("VFD-2 / P-2 75HP", 12), ("PANEL LP-1 225A", 16), ("EF-1", 20)]):
                c.line(x * inch, H - 6 * inch, x * inch, H - 9.5 * inch)
                c.rect(x * inch - 0.4 * inch, H - 8 * inch, 0.8 * inch, 0.8 * inch)
                c.drawString(x * inch - 0.9 * inch, H - 10 * inch, lab)
            if rev:
                c.rect(17.5 * inch, H - 5.5 * inch, 1.2 * inch, 0.8 * inch)
                c.drawString(17.5 * inch, H - 5.8 * inch, "SPD 480V")
            c.drawString(1.2 * inch, 2 * inch, "SEE E-201 FOR PANEL LP-1 SCHEDULE.  SEE 1/E-501 FOR GROUNDING DETAIL.")
            c.drawString(1.2 * inch, 1.5 * inch, "SCALE: NONE")
        elif sheet_no == "E-201":
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 1.5 * inch, "PANEL LP-1  208Y/120V 3PH 4W  225A MLO")
            c.setFont(SANS_B, 10)
            hdr = ["CKT", "DESCRIPTION", "BKR", "VA"]
            xs = [1.2, 2.0, 7.5, 8.5]
            for x, h in zip(xs, hdr):
                c.drawString(x * inch, H - 2.1 * inch, h)
            c.setFont(SANS, 10)
            for k in range(1, 25):
                desc = ["LIGHTING", "RECEPTACLES", "CP-1 CONTROL POWER", "HEATER", "SPARE"][k % 5]
                va = 0 if desc == "SPARE" else 400 + 60 * k
                if rev and k == 7:
                    desc, va = "SPD MONITOR", 50
                for x, v in zip(xs, [str(k), desc, "20/1", str(va)]):
                    c.drawString(x * inch, H - (2.45 + 0.28 * k) * inch, v)
            c.drawString(1.2 * inch, 2 * inch, "REFER TO E-101 FOR FEEDER.")
        elif sheet_no == "E-301":
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 1.5 * inch, "POWER PLAN - LEVEL 1")
            c.setLineWidth(3)
            c.rect(3 * inch, 4 * inch, 22 * inch, 14 * inch)
            c.setLineWidth(1.2)
            for x in range(6, 25, 4):
                c.line(x * inch, 4 * inch, x * inch, 18 * inch)
            c.setFont(SANS, 10)
            for k, (x, y) in enumerate([(5, 6), (9, 6), (13, 6), (17, 15), (21, 15)]):
                c.circle(x * inch, y * inch, 0.12 * inch)
                c.drawString(x * inch + 0.2 * inch, y * inch, f"LP-1-{2 * k + 1}")
            c.drawString(4 * inch, 3.3 * inch, 'SCALE: 1/4" = 1\'-0"')
            c.line(4 * inch, 3 * inch, 8 * inch, 3 * inch)  # scale bar 16 ft
            for k in range(5):
                c.line((4 + k) * inch, 2.9 * inch, (4 + k) * inch, 3.1 * inch)
            c.drawString(8.2 * inch, 2.95 * inch, "16 FT")
            c.drawString(12 * inch, 3.3 * inch, "SEE 2/E-501 FOR PUMP DISCONNECT MOUNTING.")
            # north arrow
            c.line(26 * inch, 16 * inch, 26 * inch, 17.2 * inch)
            c.line(26 * inch, 17.2 * inch, 25.8 * inch, 16.8 * inch)
            c.line(26 * inch, 17.2 * inch, 26.2 * inch, 16.8 * inch)
            c.drawString(25.9 * inch, 17.4 * inch, "N")
        else:
            c.setFont(SANS_B, 16)
            c.drawString(1.2 * inch, H - 1.5 * inch, "ELECTRICAL DETAILS")
            for k, lab in enumerate(["GROUNDING DETAIL", "PUMP DISCONNECT MOUNTING", "ARC FLASH LABEL"]):
                x = (1.5 + 10 * k) * inch
                c.setLineWidth(1.2)
                c.rect(x, 8 * inch, 8 * inch, 10 * inch)
                c.circle(x + 0.6 * inch, 7.4 * inch, 0.35 * inch)
                c.setFont(SANS_B, 12)
                c.drawCentredString(x + 0.6 * inch, 7.32 * inch, str(k + 1))
                c.drawString(x + 1.2 * inch, 7.35 * inch, lab)
                c.setFont(SANS, 9)
                c.drawString(x + 1.2 * inch, 7.05 * inch, "SCALE: NONE")
        c.showPage()
    c.save()


# ------------------------------------------------------------------ signed
def signed(src: Path, dst: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pyhanko.sign import signers
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PDF Studio Test Signer"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PDF Studio Corpus")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False), critical=True)
            .sign(key, hashes.SHA256()))
    kdir = OUT / "testkeys"
    kdir.mkdir(exist_ok=True)
    (kdir / "signer.key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (kdir / "signer.cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    signer = signers.SimpleSigner.load(str(kdir / "signer.key.pem"), str(kdir / "signer.cert.pem"))
    with open(src, "rb") as inf:
        w = IncrementalPdfFileWriter(inf)
        meta_ = signers.PdfSignatureMetadata(field_name="Signature1", reason="Corpus test signature")
        with open(dst, "wb") as outf:
            signers.sign_pdf(w, meta_, signer=signer, output=outf)


# ------------------------------------------------------------------ main
def build(force: bool = False) -> dict[str, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    p = {k: OUT / f"{k}.pdf" for k in [
        "letter", "scanned_letter", "scanned_rotated", "form_fillable", "form_flat", "contract_v1", "contract_v2",
        "report_tables", "slides", "encrypted", "broken", "layers", "image_heavy", "drawings_rev0", "drawings_rev1",
        "signed",
    ]}
    steps = [
        ("letter", lambda: letter_pdf(p["letter"])),
        ("scanned_letter", lambda: scanned(p["letter"], p["scanned_letter"], 1.6)),
        ("scanned_rotated", lambda: scanned(p["letter"], p["scanned_rotated"], -0.8, rotate90=True)),
        ("form_fillable", lambda: fillable_form(p["form_fillable"])),
        ("form_flat", lambda: flat_form(p["form_flat"])),
        ("contract_v1", lambda: contract(p["contract_v1"], 1)),
        ("contract_v2", lambda: contract(p["contract_v2"], 2)),
        ("report_tables", lambda: report(p["report_tables"])),
        ("slides", lambda: slides(p["slides"])),
        ("encrypted", lambda: encrypted(p["report_tables"], p["encrypted"])),
        ("broken", lambda: broken(p["report_tables"], p["broken"])),
        ("layers", lambda: layers(p["layers"])),
        ("image_heavy", lambda: image_heavy(p["image_heavy"])),
        ("drawings_rev0", lambda: drawing_set(p["drawings_rev0"], 0)),
        ("drawings_rev1", lambda: drawing_set(p["drawings_rev1"], 1)),
        ("signed", lambda: signed(p["letter"], p["signed"])),
    ]
    for key, fn in steps:
        if force or not p[key].exists():
            fn()
            print(f"built {p[key].name}  {p[key].stat().st_size / 1024:.0f} KB")
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    build(ap.parse_args().force)
