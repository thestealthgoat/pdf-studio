#!/usr/bin/env python3
"""CI license gate. Exits 1 if any installed package is GPL/AGPL/LGPL/unknown or banned.

Usage: python tools/license_audit.py [--markdown THIRD_PARTY_LICENSES.generated.md]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdfcore.licenses import audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", help="write a table of every dependency and its license")
    a = ap.parse_args()
    rows = audit()
    bad = [r for r in rows if r.verdict == "fail"]
    for r in rows:
        mark = {"ok": "ok  ", "override": "ok* ", "fail": "FAIL"}[r.verdict]
        print(f"{mark} {r.name:32} {r.version:14} {r.license[:70]}" + (f"   <- {r.reason}" if r.reason else ""))
    if a.markdown:
        lines = ["| Package | Version | License | Note |", "|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r.name} | {r.version} | {r.license.replace('|', '/')} | {r.reason} |")
        Path(a.markdown).write_text("\n".join(lines) + "\n")
    print(f"\n{len(rows)} packages, {len(bad)} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
