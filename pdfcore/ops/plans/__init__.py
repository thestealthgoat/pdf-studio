"""Drawing sets and plans (Bluebeam-style). CLI: ``pdfcore plans <subcommand> …``.

Submodules register subcommands with :func:`sub`. Reading functions (sheet detection, title blocks,
schedules) are importable on their own, e.g. ``from pdfcore.ops.plans.sheets import read_sheets``,
so other skills (UL 508A panel work) can reuse them without the CLI.

Submodules:
  sheets   — title-block parsing, sheet index + bookmarks, set reading (one-lines, schedules, notes),
             compare by sheet number, batch link callouts, slip sheets
  measure  — scale calibration, /Measure viewports, length/area/perimeter/count, takeoff → XLSX,
             sheet sizes, print to scale, tiling, layers
  cad      — DXF → PDF, PDF → DXF, electrical schematics (ladder/wiring) with wire numbers, cross-refs, BOM
  issue    — revision clouds + delta tags, issue stamps, seal placement
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import import_module
from typing import Callable

from pdfcore.ops import command

SUBMODULES = ["sheets", "measure", "cad", "issue", "site"]


@dataclass
class Sub:
    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], Callable]


SUBS: dict[str, Sub] = {}


def sub(name: str, help: str):
    """Decorator for ``configure(parser) -> runner`` functions, like pdfcore.ops.command."""

    def deco(fn):
        SUBS[name] = Sub(name, help, fn)
        return fn

    return deco


def _load() -> None:
    for m in SUBMODULES:
        try:
            import_module(f"pdfcore.ops.plans.{m}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("pdfcore.ops.plans."):
                continue
            raise


@command("plans", "Drawing sets: index, read, compare by sheet, link callouts, measure, takeoff, CAD, issue", group="plans")
def _cli(p: argparse.ArgumentParser):
    _load()
    sp = p.add_subparsers(dest="plans_cmd", metavar="<subcommand>", required=True)
    for name in sorted(SUBS):
        s = SUBS[name]
        q = sp.add_parser(name, help=s.help, description=s.help)
        q.set_defaults(_plans_runner=s.configure(q))
    return lambda ns: ns._plans_runner(ns)
