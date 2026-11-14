"""Op results and output-path rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pdfcore.gate import GateReport


def output_path(src: str | Path, tag: str, ext: str | None = None, out: str | Path | None = None) -> Path:
    """Path for a new file next to *src*: ``name.<tag><ext>``.

    Never returns an existing file and never returns *src* itself; adds ``-2``,
    ``-3``… when needed. If *out* is given it is used as-is, unless it is the
    source file (refused) or an existing directory (the derived name goes in it).
    """
    src = Path(src)
    ext = ext if ext is not None else src.suffix or ".pdf"
    if out is not None:
        out = Path(out)
        if out.is_dir():
            base = out / f"{src.stem}.{tag}{ext}"
        else:
            if out.resolve() == src.resolve():
                raise ValueError("refusing to overwrite the input file")
            return out
    else:
        base = src.with_name(f"{src.stem}.{tag}{ext}")
    cand, n = base, 2
    while cand.exists() or cand.resolve() == src.resolve():
        cand = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    return cand


@dataclass
class OpResult:
    """What every op returns."""

    output: Path | None
    gate: "GateReport | None" = None
    extra_outputs: list[Path] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.gate is None or self.gate.passed

    def line(self) -> str:
        return self.gate.line() if self.gate else "PASS"
