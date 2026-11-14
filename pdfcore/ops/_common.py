"""Helpers shared by ops: page-range parsing, finalize-with-gate, metadata carry-over."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable, Sequence

from pdfcore.gate import GateOptions, run_gate
from pdfcore.result import OpResult, output_path

__all__ = ["parse_pages", "finalize", "output_path", "add_common", "gate_options_from"]


def parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-3,5,8-' → 0-based indexes. Supports 'odd', 'even', 'last', negative from end ('-1' = last)."""
    if not spec or spec.strip().lower() in ("all", "*"):
        return list(range(n))
    s = spec.strip().lower()
    if s == "odd":
        return list(range(0, n, 2))
    if s == "even":
        return list(range(1, n, 2))
    out: list[int] = []
    for part in re.split(r"\s*,\s*", s):
        if not part:
            continue
        part = part.replace("last", str(n))
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m and (m.group(1) or m.group(2)) and not re.fullmatch(r"-\d+", part):
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
            step = 1 if b >= a else -1
            out.extend(i - 1 for i in range(a, b + step, step))
        elif re.fullmatch(r"-\d+", part):
            out.append(n + int(part))
        else:
            out.append(int(part) - 1)
    bad = [i + 1 for i in out if not (0 <= i < n)]
    if bad:
        raise ValueError(f"page(s) {bad} out of range 1–{n}")
    return out


def gate_options_from(ns: argparse.Namespace | None = None, **over) -> GateOptions:
    o = GateOptions()
    if ns is not None:
        if getattr(ns, "gate_profile", None):
            o.profile = ns.gate_profile
        if getattr(ns, "size_budget", None):
            o.max_size_mb = ns.size_budget
        if getattr(ns, "png_dir", None):
            o.png_dir = Path(ns.png_dir)
    for k, v in over.items():
        setattr(o, k, v)
    return o


def finalize(
    out: Path,
    baseline: Path | Sequence[Path] | None,
    *,
    options: GateOptions | None = None,
    password: str | None = None,
    extra: Iterable[Path] = (),
    data: dict | None = None,
    quiet: bool = False,
) -> OpResult:
    """Run the quality gate on *out*, print the one-line verdict, return an OpResult."""
    rep = run_gate(out, baseline=baseline, options=options, password=password)
    res = OpResult(out, rep, list(extra), data or {})
    if not quiet:
        print(f"{out}  {rep.line()}")
    return res


def add_common(p: argparse.ArgumentParser, *, out: bool = True) -> None:
    if out:
        p.add_argument("-o", "--out", help="output file (default: next to the input, never overwriting)")
    p.add_argument("--password", help="password to open the input")
    p.add_argument("--size-budget", type=float, metavar="MB", help="fail the gate if output exceeds this size")
    p.add_argument("--png-dir", help="save gate page renders here for visual inspection")
    p.add_argument("--gate-profile", choices=["edit", "typeset", "drawing"], help="quality-gate profile")
