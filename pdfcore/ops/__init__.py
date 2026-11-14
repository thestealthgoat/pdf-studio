"""Operations. Each module registers CLI commands with :func:`command`.

Library use: call the plain functions in each module (``pdfcore.ops.organize.merge``…).
CLI use: ``pdfcore <command> …``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import import_module
from typing import Callable

from pdfcore.result import OpResult

MODULES = [
    "organize", "navigate", "stamp", "redact", "markup", "compare", "ocr", "forms",
    "optimize", "security", "extract", "convert", "recipes_op", "typeset", "plans", "sign", "standards",
]


@dataclass
class Command:
    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], OpResult | None]
    group: str


COMMANDS: dict[str, Command] = {}


def command(name: str, help: str, group: str = ""):
    """Decorator: ``@command("merge", "Merge PDFs")`` on a ``configure(parser)`` function that returns the runner."""

    def deco(configure):
        def _run(ns):
            return ns._runner(ns)

        COMMANDS[name] = Command(name, help, configure, _run, group)
        return configure

    return deco


def load_all() -> None:
    for m in MODULES:
        try:
            import_module(f"pdfcore.ops.{m}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("pdfcore.ops."):
                continue  # module not built yet
            raise
