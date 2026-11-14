"""``pdfcore`` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pdfcore import __version__


def build_parser() -> argparse.ArgumentParser:
    from pdfcore.ops import COMMANDS, load_all

    load_all()
    p = argparse.ArgumentParser(prog="pdfcore", description="PDF Studio toolkit. Every op writes a new file and ends with PASS/FAIL.")
    p.add_argument("--version", action="version", version=f"pdfcore {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    d = sub.add_parser("doctor", help="report what is installed and what is missing")
    d.add_argument("--json", action="store_true")
    d.add_argument("--strict", action="store_true", help="exit 1 if any required component is missing")
    i = sub.add_parser("install", help="install Python extras, external tools and fonts")
    i.add_argument("--yes", action="store_true", help="do not prompt")
    i.add_argument("--no-system", action="store_true", help="skip Homebrew/apt tools")
    i.add_argument("--no-fonts", action="store_true")
    g = sub.add_parser("gate", help="run the quality gate on a PDF")
    g.add_argument("pdf")
    g.add_argument("--baseline")
    g.add_argument("--profile", default="edit", choices=["edit", "typeset", "drawing"])
    g.add_argument("--standard")
    g.add_argument("--png-dir")
    g.add_argument("--size-budget", type=float)
    g.add_argument("--min-dpi", type=float)
    g.add_argument("--verbose", "-v", action="store_true")
    g.add_argument("--password")
    e = sub.add_parser("engines", help="list installed engines")

    for name in sorted(COMMANDS):
        c = COMMANDS[name]
        sp = sub.add_parser(name, help=c.help, description=c.help)
        runner = c.configure(sp)
        sp.set_defaults(_runner=runner)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    ns = p.parse_args(argv)
    if not ns.cmd:
        p.print_help()
        return 0
    try:
        if ns.cmd == "doctor":
            from pdfcore.doctor import run_doctor

            return run_doctor(as_json=ns.json, strict=ns.strict)
        if ns.cmd == "install":
            from pdfcore.installer import run_install

            return run_install(yes=ns.yes, system=not ns.no_system, fonts=not ns.no_fonts)
        if ns.cmd == "engines":
            from pdfcore import engines

            for k in engines.KINDS:
                print(f"{k}: {', '.join(engines.available(k))}")
            return 0
        if ns.cmd == "gate":
            from pdfcore.gate import GateOptions, run_gate

            o = GateOptions(profile=ns.profile, standard=ns.standard, png_dir=Path(ns.png_dir) if ns.png_dir else None,
                            max_size_mb=ns.size_budget, min_image_dpi=ns.min_dpi)
            rep = run_gate(ns.pdf, baseline=ns.baseline, options=o, password=ns.password)
            print(rep.details() if ns.verbose else rep.line())
            return 0 if rep.passed else 1
        res = ns._runner(ns)
        if res is None:
            return 0
        return 0 if res.ok else 1
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
