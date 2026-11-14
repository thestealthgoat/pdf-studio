"""Markdown and plain text → typeset PDF in the house style (Typst ``report`` shell via :func:`typeset.make`).

The Markdown reader covers CommonMark/GFM as used in practice: YAML front matter (title, subtitle,
author, date, client, rev, kind, toc, numbered), ATX and setext headings, paragraphs with hard breaks,
nested bullet/numbered/task lists, block quotes, fenced and indented code, pipe tables with alignment,
horizontal rules, images (local files are embedded; remote ones become links), links, autolinks,
bold/italic/strike, inline code and backslash escapes. Every piece of text is escaped for Typst, so any
character in the source is printed literally (``typeset.md_to_typst`` is a much smaller converter meant
for short prose fields in YAML data and does not escape).

Code is set in the house sans (the house fonts carry no monospace face, and the gate fails on Typst's
fallback fonts). Plain ``.txt`` gets the same shell: paragraphs reflowed, short-line blocks (addresses,
lists, tables made of spaces) kept line by line.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["markdown_to_typst", "text_to_typst", "markdown_to_pdf", "text_to_pdf", "typst_escape", "typst_str"]

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_SPECIAL = re.compile(r"([\\#$*_`<>@\[\]~/])")


def typst_escape(s: str) -> str:
    """Escape text for Typst markup so every character prints literally."""
    return _SPECIAL.sub(r"\\\1", s)


def _line_start(s: str) -> str:
    """Escape what would start a list, enum or heading at the beginning of a markup line."""
    if re.match(r"^\s*[-+=]", s):
        return re.sub(r"^(\s*)", r"\1\\", s, count=1)
    return re.sub(r"^(\s*\d+)\.", r"\1\\.", s, count=1)


def typst_str(s: str) -> str:
    """A Typst string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n").replace("\t", "\\t") + '"'


# ---------------------------------------------------------------- inline Markdown


_INLINE = re.compile(
    r"(?P<code>(?P<ticks>`+)(?P<codebody>.+?)(?P=ticks))"
    r"|(?P<img>!\[(?P<ialt>[^\]]*)\]\((?P<isrc><[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<link>\[(?P<ltext>(?:[^\[\]]|\[[^\]]*\])*)\]\((?P<lurl><[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<auto><(?P<aurl>(?:https?|ftp|mailto):[^>\s]+)>)"
    r"|(?P<url>(?<![\w/])(?:https?://|www\.)[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"])"
    r"|(?P<strong>(?P<sd>\*\*|__)(?=\S)(?P<sbody>.+?)(?<=\S)(?P=sd))"
    r"|(?P<strike>~~(?=\S)(?P<kbody>.+?)(?<=\S)~~)"
    r"|(?P<em>\*(?=[^\s*])(?P<ebody>(?:[^*]|\*\*[^*]+\*\*)+?)(?<=[^\s*])\*|(?<![\w])_(?=\S)(?P<ubody>.+?)(?<=\S)_(?![\w]))"
    r"|(?P<br><br\s*/?>)"
    r"|(?P<tag></?[A-Za-z][^>]*>)"
    r"|(?P<esc>\\(?P<ec>[!-/:-@\[-`{-~]))"
)


@dataclass
class _Ctx:
    base: Path | None
    assets: list[tuple[Path, str]] = field(default_factory=list)  # (source file, name in /assets)
    sans: str = "Inter"

    def asset(self, ref: str) -> str | None:
        ref = ref.strip("<>")
        if re.match(r"^[a-z]+:", ref) or self.base is None:
            return None
        p = (self.base / ref).resolve()
        if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
            return None
        for src, name in self.assets:
            if src == p:
                return name
        name = f"img{len(self.assets) + 1}{p.suffix.lower()}"
        self.assets.append((p, name))
        return name


def inline(s: str, ctx: _Ctx) -> str:
    """Inline Markdown → Typst markup. Embedded expressions end with ``;`` so following text never continues them."""
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(s):
        out.append(typst_escape(s[pos:m.start()]))
        pos = m.end()
        if m.group("code") is not None:
            body = m.group("codebody")
            if body.startswith(" ") and body.endswith(" ") and body.strip():
                body = body[1:-1]
            out.append(f"#box(fill: luma(242), inset: (x: 2pt), outset: (y: 2.5pt), radius: 1.5pt, raw({typst_str(body)}));")
        elif m.group("img") is not None:
            name = ctx.asset(m.group("isrc"))
            alt = m.group("ialt")
            if name:
                out.append(f'#box(image("/assets/{name}", height: 1.2em, alt: {typst_str(alt or name)}));')
            else:
                url = m.group("isrc").strip("<>")
                out.append(f"#link({typst_str(url)})[{typst_escape(alt or url)}];")
        elif m.group("link") is not None:
            url = m.group("lurl").strip("<>")
            out.append(f"#link({typst_str(url)})[{inline(m.group('ltext'), ctx)}];")
        elif m.group("auto") is not None:
            url = m.group("aurl")
            out.append(f"#link({typst_str(url)})[{typst_escape(re.sub(r'^mailto:', '', url))}];")
        elif m.group("url") is not None:
            url = m.group("url")
            href = url if url.startswith("http") else "https://" + url
            out.append(f"#link({typst_str(href)})[{typst_escape(url)}];")
        elif m.group("strong") is not None:
            out.append(f"#strong[{inline(m.group('sbody'), ctx)}];")
        elif m.group("strike") is not None:
            out.append(f"#strike[{inline(m.group('kbody'), ctx)}];")
        elif m.group("em") is not None:
            out.append(f"#emph[{inline(m.group('ebody') or m.group('ubody'), ctx)}];")
        elif m.group("br") is not None:
            out.append("#linebreak();")
        elif m.group("tag") is not None:
            pass  # other inline HTML tags: keep their text, drop the tag
        elif m.group("esc") is not None:
            out.append(typst_escape(m.group("ec")))
    out.append(typst_escape(s[pos:]))
    return "".join(out)


def plain(s: str) -> str:
    """Inline Markdown → plain text (for headings)."""
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|~~|`+)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<!\w)[*_](\S.*?\S|\S)[*_](?!\w)", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)
    return s.strip()


# ---------------------------------------------------------------- blocks


_FENCE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})\s*([\w+#.-]*)")
_ATX = re.compile(r"^\s{0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TSEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _table(lines: list[str], ctx: _Ctx) -> str:
    header = _cells(lines[0])
    seps = _cells(lines[1])
    n = len(header)
    aligns = []
    for k in range(n):
        sp = seps[k] if k < len(seps) else "-"
        aligns.append("center" if sp.startswith(":") and sp.endswith(":") else "right" if sp.endswith(":") else "left")
    body = [_cells(r) for r in lines[2:]]
    longest = max([len(" ".join(header))] + [len(" ".join(r)) for r in body])
    cell = lambda c: "[" + inline(c, ctx) + "]"  # noqa: E731
    parts = [f"columns: {n}", f"align: ({', '.join(aligns)},)",
             "stroke: (x, y) => if y == 0 { (bottom: 0.75pt) } else { (bottom: 0.4pt + luma(200)) }",
             "inset: (x: 5pt, y: 5pt)",
             "table.header(" + ", ".join(f"[#strong[{inline(h, ctx)}]]" for h in header) + ")"]
    for r in body:
        r = (r + [""] * n)[:n]
        parts.append(", ".join(cell(c) for c in r))
    t = "table(" + ",\n  ".join(parts) + ")"
    return f"#wide({t})" if n > 3 or longest > 70 else f"#{t}"


def _code(text: str, lang: str) -> str:
    la = f", lang: {typst_str(lang)}" if lang else ""
    return ("#block(fill: luma(246), inset: 8pt, radius: 2pt, width: 100%, breakable: true, "
            f"raw({typst_str(text)}, block: true{la}))")


def _image_block(alt: str, ref: str, ctx: _Ctx) -> str:
    name = ctx.asset(ref)
    if not name:
        url = ref.strip("<>")
        return f"#link({typst_str(url)})[{typst_escape(alt or url)}];"
    w = _natural_width_in(next(s for s, nm in ctx.assets if nm == name))
    width = "100%" if w is None or w > 4.6 else f"{w:.2f}in"
    img = f'image("/assets/{name}", width: {width}, alt: {typst_str(alt or name)})'
    if alt:
        return f"#figure({img}, caption: [{inline(alt, ctx)}])"
    return f"#figure({img})"


def _natural_width_in(p: Path) -> float | None:
    if p.suffix.lower() == ".svg":
        return None
    try:
        from PIL import Image

        with Image.open(p) as im:
            dpi = im.info.get("dpi", (72, 72))[0] or 72
            return im.size[0] / float(dpi if dpi >= 10 else 72)
    except Exception:
        return None


def _blocks(lines: list[str], ctx: _Ctx, heads: list[tuple[int, str]]) -> list[str]:
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        nonlocal para
        if not para:
            return
        txt_lines = []
        for k, ln in enumerate(para):
            hard = ln.endswith("  ") or (ln.endswith("\\") and not ln.endswith("\\\\"))
            s = ln.rstrip()
            if s.endswith("\\") and hard:
                s = s[:-1]
            conv = inline(s.strip(), ctx)
            if k == 0:
                conv = _line_start(conv)
            txt_lines.append(conv + (" \\" if hard and k < len(para) - 1 else ""))
        out.append("\n".join(_line_start(t) for t in txt_lines))
        para = []

    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            flush()
            i += 1
            continue
        m = _FENCE.match(ln)
        if m:
            flush()
            fence, lang = m.group(2), m.group(3)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence[0] * len(fence)):
                body.append(lines[i][len(m.group(1)):] if lines[i].startswith(m.group(1)) else lines[i])
                i += 1
            i += 1
            out.append(_code("\n".join(body), lang))
            continue
        if re.match(r"^\s*<!--", ln):
            flush()
            while i < len(lines) and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        m = _ATX.match(ln)
        if m:
            flush()
            level, text = len(m.group(1)), plain(m.group(2) or "")
            heads.append((level, text))
            out.append("=" * level + " #" + typst_str(text) + ";")
            i += 1
            continue
        if para and _SETEXT.match(ln) and not _ITEM.match(para[-1]):
            level = 1 if ln.strip().startswith("=") else 2
            text = plain(" ".join(p.strip() for p in para))
            para = []
            heads.append((level, text))
            out.append("=" * level + " #" + typst_str(text) + ";")
            i += 1
            continue
        if _HR.match(ln):
            flush()
            out.append("#line(length: 100%, stroke: 0.5pt + luma(190))")
            i += 1
            continue
        if "|" in ln and i + 1 < len(lines) and _TSEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            rows = [ln, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            out.append(_table(rows, ctx))
            continue
        if re.match(r"^\s{0,3}>", ln):
            flush()
            q = []
            while i < len(lines) and re.match(r"^\s{0,3}>", lines[i]):
                q.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            inner = "\n\n".join(_blocks(q, ctx, []))
            out.append(f"#block(inset: (left: 10pt, y: 2pt), stroke: (left: 1.5pt + luma(190)))[#emph[{inner}]]")
            continue
        mi = _ITEM.match(ln)
        if mi:
            flush()
            stack: list[int] = []
            items: list[str] = []
            while i < len(lines):
                cur = lines[i]
                mi = _ITEM.match(cur)
                if mi:
                    ind = len(mi.group(1).expandtabs(4))
                    while stack and ind < stack[-1]:
                        stack.pop()
                    if not stack or ind > stack[-1] + 1:
                        if not stack or ind > stack[-1]:
                            stack.append(ind)
                    depth = len(stack) - 1
                    marker = mi.group(2)
                    text = mi.group(3)
                    task = re.match(r"^\[([ xX])\]\s+(.*)$", text)
                    if task:
                        text = ("[x] " if task.group(1).lower() == "x" else "[ ] ") + task.group(2)
                    mk = "-" if marker[0] in "-*+" else marker[:-1] + "."
                    items.append("  " * depth + mk + " " + inline(text, ctx))
                    i += 1
                    continue
                if cur.strip() and (cur.startswith((" ", "\t"))) and items:
                    items[-1] += " " + inline(cur.strip(), ctx)
                    i += 1
                    continue
                if not cur.strip() and i + 1 < len(lines) and (_ITEM.match(lines[i + 1]) or lines[i + 1].startswith(("  ", "\t"))):
                    i += 1
                    continue
                break
            out.append("\n".join(items))
            continue
        if ln.startswith(("    ", "\t")) and not para:
            body = []
            while i < len(lines) and (lines[i].startswith(("    ", "\t")) or not lines[i].strip()):
                body.append(lines[i][4:] if lines[i].startswith("    ") else lines[i].lstrip("\t"))
                i += 1
            out.append(_code("\n".join(body).rstrip("\n"), ""))
            continue
        mimg = re.match(r"^\s*!\[([^\]]*)\]\((<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\)\s*$", ln)
        if mimg and not para:
            out.append(_image_block(mimg.group(1), mimg.group(2), ctx))
            i += 1
            continue
        para.append(ln)
        i += 1
    flush()
    return out


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n(---|\.\.\.)\s*(\n|$)", text, re.S)
        if m:
            import yaml

            try:
                meta = yaml.safe_load(m.group(1)) or {}
                if isinstance(meta, dict):
                    return meta, text[m.end():]
            except Exception:
                pass
    return {}, text


def markdown_to_typst(text: str, *, base: Path | None = None, sans: str = "Inter") -> tuple[str, dict[str, Any], list[tuple[Path, str]]]:
    """(Typst markup for the body, metadata, [(image file, asset name)]). The title comes from front matter or a
    leading level-1 heading (which is then removed from the body)."""
    meta, body = _front_matter(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = body.split("\n")
    if "title" not in meta:
        k = next((j for j, ln in enumerate(lines) if ln.strip()), None)
        if k is not None:
            m = _ATX.match(lines[k])
            if m and len(m.group(1)) == 1:
                meta["title"] = plain(m.group(2) or "")
                lines = lines[:k] + lines[k + 1:]
            elif k + 1 < len(lines) and re.match(r"^\s{0,3}=+\s*$", lines[k + 1]):
                meta["title"] = plain(lines[k])
                lines = lines[:k] + lines[k + 2:]
    ctx = _Ctx(base, sans=sans)
    heads: list[tuple[int, str]] = []
    blocks = _blocks(lines, ctx, heads)
    pre = f"#show raw: set text(font: {typst_str(sans)}, size: 0.9em)\n"
    meta.setdefault("_headings", len(heads))
    return pre + "\n\n".join(blocks) + "\n", meta, ctx.assets


def text_to_typst(text: str) -> tuple[str, str | None]:
    """(Typst markup, title or None) for a plain-text file: blank lines separate paragraphs; long-line paragraphs
    are reflowed, short-line ones (addresses, lists, aligned columns) keep their line breaks and indentation."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    pages = text.split("\f")
    out_pages: list[str] = []
    title = None
    for pno, pg in enumerate(pages):
        paras = [p for p in re.split(r"\n\s*\n", pg) if p.strip()]
        if pno == 0 and len(paras) > 1:
            first = paras[0].strip()
            if "\n" not in first and len(first) <= 80:
                title = first
                paras = paras[1:]
        blocks = []
        for p in paras:
            lns = [ln.rstrip() for ln in p.strip("\n").split("\n")]
            avg = sum(len(x) for x in lns) / max(1, len(lns))
            structured = any(re.match(r"^\s+\S|^\s*([-*•+]|\d+[.)])\s", x) for x in lns) or "   " in p.strip()
            if len(lns) > 1 and avg >= 45 and not structured:
                joined = " ".join(x.strip() for x in lns)
                blocks.append(_line_start(typst_escape(joined)))
            else:
                rows = []
                for x in lns:
                    ind = len(x) - len(x.lstrip(" "))
                    body = _line_start(typst_escape(x.strip()))
                    body = re.sub(r" {2,}", lambda m: f"#h({len(m.group(0)) * 0.3:.1f}em);", body)
                    rows.append((f"#h({ind * 0.3:.1f}em);" if ind else "") + body)
                blocks.append(" \\\n".join(rows))
        out_pages.append("\n\n".join(blocks))
    return "\n\n#pagebreak()\n\n".join(out_pages) + "\n", title


# ---------------------------------------------------------------- → PDF


def _make(src: Path, body: str, meta: dict[str, Any], assets: list[tuple[Path, str]], dst: Path, *,
          brand: str = "default", kind: str = "Document", quiet: bool = False):
    from pdfcore.ops.typeset import make

    data: dict[str, Any] = {
        "title": str(meta.get("title") or src.stem.replace("_", " ").replace("-", " ").strip() or src.name),
        "kind": str(meta.get("kind", kind)),
        "format": "typst",
        "numbered": bool(meta.get("numbered", False)),
        "toc": bool(meta.get("toc", meta.get("_headings", 0) >= 6)),
        "body": body,
    }
    for k in ("subtitle", "author", "date", "client", "rev", "summary"):
        if meta.get(k) not in (None, ""):
            v = meta[k]
            data[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    if "summary" in data:
        data["summary"] = inline(data["summary"], _Ctx(None))
    with tempfile.TemporaryDirectory(prefix="pdfcore-md-") as tmp:
        t = Path(tmp)
        for f, name in assets:
            shutil.copy(f, t / name)
        data["images"] = [name for _, name in assets]
        yml = t / "data.json"
        yml.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return make("report", yml, brand=brand, out=dst, quiet=quiet)


def markdown_to_pdf(src: str | Path, *, out: str | Path | None = None, brand: str = "default", quiet: bool = False):
    """Markdown → PDF in the house report style (title block, headings, tables, code, images)."""
    from pdfcore.ops.convert import _pdf_out
    from pdfcore.ops.typeset import load_brand

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    sans = load_brand(brand).get("fonts", {}).get("sans", "Inter")
    body, meta, assets = markdown_to_typst(src.read_text(encoding="utf-8-sig", errors="replace"), base=src.parent, sans=sans)
    return _make(src, body, meta, assets, _pdf_out(src, out), brand=brand, quiet=quiet)


def text_to_pdf(src: str | Path, *, out: str | Path | None = None, brand: str = "default", quiet: bool = False):
    """Plain text → PDF in the house style (serif body, no monospace)."""
    from pdfcore.ops.convert import _pdf_out

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    raw = src.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    body, title = text_to_typst(text)
    meta: dict[str, Any] = {"title": title} if title else {}
    return _make(src, body, meta, [], _pdf_out(src, out), brand=brand, kind="Text", quiet=quiet)
