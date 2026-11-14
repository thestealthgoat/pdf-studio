"""Professional documents from templates (Typst). ``pdfcore make <template> data.yaml``.

Templates live in ``templates/typst/<name>.typ`` and share ``house.typ`` (grid, type scale, tables,
running header/footer). Brands are token files in ``templates/brands/<brand>.toml``.

Data is YAML/JSON. Fields documented in each example under ``templates/examples/``. Money is computed
here with Decimal and passed to Typst as formatted strings, so totals are exact.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
import tomllib
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from pdfcore.fonts import font_dir, templates_dir
from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize
from pdfcore.result import OpResult

TEMPLATES = ["estimate", "invoice", "report", "proposal", "letter", "memo", "certificate", "runsheet",
             "production", "booklet", "manuscript", "drawing-sheet"]
SYMBOLS = {"USD": "$", "CAD": "$", "EUR": "€", "GBP": "£", "MXN": "$"}

# ----------------------------------------------------------------------------- brand
def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def brand_path(brand: str | Path) -> Path:
    p = Path(brand)
    if p.suffix == ".toml" and p.exists():
        return p
    cand = templates_dir() / "brands" / f"{brand}.toml"
    if not cand.exists():
        avail = sorted(x.stem for x in (templates_dir() / "brands").glob("*.toml"))
        raise FileNotFoundError(f"brand {brand!r} not found; available: {avail}")
    return cand


def load_brand(brand: str | Path = "default") -> dict:
    p = brand_path(brand)
    b = tomllib.loads(p.read_text())
    c = b["colors"]
    problems = []
    for key in ("ink", "muted", "primary"):
        r = contrast(c[key], c["paper"])
        if r < 4.5:
            problems.append(f"{key} on paper is {r:.2f}:1 (needs 4.5:1)")
    if contrast("#ffffff", c["primary"]) < 4.5:
        problems.append("white on primary is below 4.5:1")
    if problems:
        raise ValueError(f"brand {p.stem}: " + "; ".join(problems))
    b["_dir"] = str(p.parent)
    return b


# ----------------------------------------------------------------------------- data helpers
def _d(x) -> Decimal:
    return Decimal(str(x))


def _cents(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_money(x: Decimal, symbol: str = "", parens: bool = False) -> str:
    x = _cents(x)
    s = f"{abs(x):,.2f}"
    if x < 0:
        return f"({symbol}{s})" if parens else f"−{symbol}{s}"
    return f"{symbol}{s}"


def fmt_qty(q) -> str:
    d = _d(q)
    return f"{d:,.0f}" if d == d.to_integral() else f"{d.normalize():,f}"


def fmt_date(v) -> str:
    if isinstance(v, str):
        try:
            v = dt.date.fromisoformat(v)
        except ValueError:
            return v
    if isinstance(v, dt.datetime):
        v = v.date()
    if isinstance(v, dt.date):
        return f"{v:%B} {v.day}, {v.year}"
    return str(v)


def md_to_typst(s: str) -> str:
    """Small Markdown → Typst markup converter for prose fields (headings, bold, italic, links, lists, code)."""
    if not s:
        return s
    out = []
    for line in s.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            line = "=" * len(m.group(1)) + " " + m.group(2)
        line = re.sub(r"^(\s*)\*\s+", r"\1- ", line)
        line = re.sub(r"^(\s*)\d+[.)]\s+", r"\1+ ", line)
        out.append(line)
    t = "\n".join(out)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f'#link("{m.group(2)}")[{m.group(1)}]', t)
    # **bold** → *bold* (Typst strong). Italics: use _underscores_ (same in Markdown and Typst).
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
    t = re.sub(r"(?<![\\$])\$", r"\\$", t)  # dollar signs are literal in prose
    t = t.replace("@", "\\@")
    return t


def _prose(data: dict, keys: list[str]) -> None:
    if data.get("format", "markdown") == "markdown":
        for k in keys:
            v = data.get(k)
            if isinstance(v, str):
                data[k] = md_to_typst(v)
            elif isinstance(v, list):
                data[k] = [md_to_typst(x) if isinstance(x, str) else x for x in v]


def prepare_invoice(data: dict) -> dict:
    d = dict(data)
    d["kind"] = "Invoice"
    if "due" in d:
        d["valid_until"] = d.pop("due")
    else:
        d["valid_days"] = d.get("due_days", 30)
    d.setdefault("acceptance", False)
    return prepare_estimate(d)


def prepare_proposal(data: dict) -> dict:
    d = prepare_generic(data)
    pr = d.get("pricing")
    if pr:
        sym = SYMBOLS.get(pr.get("currency", "USD"), "")
        items = [{"description": i["description"], "amount": fmt_money(_d(i["amount"]))} for i in pr.get("items", [])]
        total = sum((_d(i["amount"]) for i in pr.get("items", [])), Decimal(0))
        d["pricing"] = {**pr, "symbol": sym, "items": items, "total": fmt_money(total, sym),
                        "note": md_to_typst(pr.get("note", "")) if d.get("format", "markdown") == "markdown" else pr.get("note", "")}
    return d


def prepare_estimate(data: dict) -> dict:
    d = dict(data)
    d.setdefault("kind", "Estimate")
    d.setdefault("currency", "USD")
    d["symbol"] = SYMBOLS.get(d["currency"], "")
    date = d.get("date") or dt.date.today()
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    d["date"] = fmt_date(date)
    if "valid_until" in d:
        d["valid_until"] = fmt_date(d["valid_until"])
    else:
        d["valid_until"] = fmt_date(date + dt.timedelta(days=int(d.get("valid_days", 30))))
    amounts: dict[str, Decimal] = {}
    subtotal = Decimal(0)
    secs = []
    for sec in d.get("sections", []):
        st = Decimal(0)
        items = []
        for it in sec.get("items", []):
            amt = _cents(_d(it.get("qty", 1)) * _d(it.get("unit_price", 0))) if "amount" not in it else _cents(_d(it["amount"]))
            code = str(it.get("code", ""))
            amounts[code] = amt
            st += amt
            items.append({**it, "code": code, "qty": fmt_qty(it.get("qty", 1)), "unit": it.get("unit", ""),
                          "unit_price": fmt_money(_d(it.get("unit_price", 0))), "amount": fmt_money(amt),
                          "note": it.get("note", "")})
        subtotal += st
        secs.append({"name": sec.get("name", ""), "items": items, "subtotal": fmt_money(st)})
    d["sections"] = secs
    adjs, total = [], subtotal
    for a in d.get("adjustments", []):
        if "rate" in a:
            applies = a.get("applies_to", "all")
            base = subtotal if applies == "all" else sum((amounts.get(str(c), Decimal(0)) for c in applies), Decimal(0))
            amt = _cents(base * _d(a["rate"]))
        else:
            amt = _cents(_d(a.get("amount", 0)))
        total += amt
        adjs.append({"label": a.get("label", ""), "amount": fmt_money(amt, d["symbol"], parens=True)})
    d["totals"] = {"subtotal": fmt_money(subtotal, d["symbol"]), "adjustments": adjs, "total": fmt_money(total, d["symbol"]),
                   "_total": str(_cents(total))}
    d["alternates"] = [{**a, "code": str(a.get("code", "")), "amount": fmt_money(_d(a.get("amount", 0)), parens=True)}
                       for a in d.get("alternates", [])]
    d.setdefault("prepared_by", {"name": ""})
    d.setdefault("project", {"name": d.get("title", "")})
    _prose(d, ["summary", "inclusions", "exclusions", "clarifications", "terms"])
    return d


def prepare_generic(data: dict) -> dict:
    d = dict(data)
    for k in ("date", "due", "due_date", "event_date"):
        if k in d:
            d[k] = fmt_date(d[k])
    _prose(d, ["summary", "body", "intro", "closing_note", "notes", "terms"])
    if isinstance(d.get("sections"), list):
        for s in d["sections"]:
            if isinstance(s, dict) and isinstance(s.get("body"), str) and d.get("format", "markdown") == "markdown":
                s["body"] = md_to_typst(s["body"])
    return d


PREPARE = {"estimate": prepare_estimate, "invoice": prepare_invoice, "proposal": prepare_proposal}


def _jsonable(o: Any):
    if isinstance(o, (dt.date, dt.datetime)):
        return fmt_date(o)
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


# ----------------------------------------------------------------------------- render
def _out_for(data_path: Path | None, template: str, out) -> Path:
    if out:
        return Path(out)
    base = data_path.with_suffix(".pdf") if data_path else Path.cwd() / f"{template}.pdf"
    cand, n = base, 2
    while cand.exists():
        cand = base.with_name(f"{base.stem}-{n}.pdf")
        n += 1
    return cand


def make(
    template: str,
    data: dict | str | Path,
    *,
    brand: str | Path = "default",
    out: str | Path | None = None,
    standard: str | None = None,
    png_dir: str | Path | None = None,
    quiet: bool = False,
) -> OpResult:
    import typst

    tdir = templates_dir()
    if not (tdir / "typst" / f"{template}.typ").exists():
        raise ValueError(f"unknown template {template!r}; available: {sorted(p.stem for p in (tdir / 'typst').glob('*.typ') if p.stem != 'house')}")
    data_path = None
    if not isinstance(data, dict):
        data_path = Path(data)
        data = yaml.safe_load(data_path.read_text()) or {}
    b = load_brand(brand)
    prepared = PREPARE.get(template, prepare_generic)(data)
    dst = _out_for(data_path, template, out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-typeset-") as tmp:
        root = Path(tmp)
        shutil.copytree(tdir / "typst", root / "typst")
        shutil.copytree(Path(b["_dir"]), root / "brands")
        # images referenced by the data, relative to the data file
        if data_path is not None:
            for k in ("images", "assets"):
                for rel in prepared.get(k, []) or []:
                    srcf = (data_path.parent / rel).resolve()
                    if srcf.exists():
                        (root / "assets").mkdir(exist_ok=True)
                        shutil.copy(srcf, root / "assets" / srcf.name)
        brand_json = {k: v for k, v in b.items() if not k.startswith("_")}
        (root / "data.json").write_text(json.dumps(prepared, default=_jsonable, ensure_ascii=False))
        (root / "brand.json").write_text(json.dumps(brand_json, ensure_ascii=False))
        fn = template.replace("-", "_")
        (root / "main.typ").write_text(
            f'#import "/typst/{template}.typ": {fn}\n#{fn}(data: json("/data.json"), brand: json("/brand.json"))\n'
        )
        kw: dict[str, Any] = dict(root=str(root), font_paths=[str(font_dir())], ignore_system_fonts=True, pdf_tags=True)
        if standard:
            kw["pdf_standards"] = [standard.lower().replace("pdfa-", "a-").replace("pdfua-", "ua-")]
        pdf_bytes = typst.compile(str(root / "main.typ"), **kw)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(pdf_bytes)
    o = GateOptions(profile="typeset", margins_in=1.0, min_image_dpi=150, require_metadata=True,
                    png_dir=Path(png_dir) if png_dir else None, png_dpi=110 if png_dir else 60,
                    standard=standard)
    extra = {"total": prepared.get("totals", {}).get("_total")} if "totals" in prepared else {}
    return finalize(dst, None, options=o, data=extra, quiet=quiet)


@command("make", "Typeset a professional document from a template + YAML data (estimate, report, proposal, …)", group="make")
def _cli(p):
    p.add_argument("template", choices=sorted(x.stem for x in (templates_dir() / "typst").glob("*.typ") if x.stem != "house"))
    p.add_argument("data", help="YAML or JSON data file (see templates/examples/)")
    p.add_argument("--brand", default="default", help="brand name in templates/brands/ or a .toml path")
    p.add_argument("--standard", help="pdfa-2b, pdfa-3b, pdfua-1 …")
    add_common(p)
    return lambda ns: make(ns.template, ns.data, brand=ns.brand, out=ns.out, standard=ns.standard, png_dir=ns.png_dir)
