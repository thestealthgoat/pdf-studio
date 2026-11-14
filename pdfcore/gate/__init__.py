"""Quality gate — runs after every op and prints one line: PASS or FAIL p.N: problem (fix: …).

The gate compares the output with a *baseline* (normally the op's input).
Problems already present in the baseline are reported as warnings, not
failures, so a flawed source file does not make every edit fail. Problems
the op introduced fail the gate.

Profiles pick which checks are strict:

``edit``      (default) existing documents: fonts, clipping, blank pages, links, redaction.
``typeset``   documents we generated: adds margins, headings at page foot, metadata, image dpi.
``drawing``   drawing sheets: like ``edit`` plus minimum text height.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from pdfcore.gate import checks
from pdfcore.gate.checks import Finding, GateOptions

__all__ = ["GateReport", "GateOptions", "Finding", "run_gate"]


@dataclass
class GateReport:
    path: Path
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    pngs: list[Path] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def line(self) -> str:
        if self.passed:
            return "PASS"
        f = self.failures[0]
        where = f"p.{f.page + 1}: " if f.page is not None else ""
        more = f" (+{len(self.failures) - 1} more)" if len(self.failures) > 1 else ""
        fix = f" (fix: {f.fix})" if f.fix else ""
        return f"FAIL {where}{f.message}{fix}{more}"

    def details(self) -> str:
        rows = [self.line()]
        for f in self.failures:
            rows.append(f"  FAIL  {f.where()}{f.message}" + (f"  → {f.fix}" if f.fix else ""))
        for f in self.warnings:
            rows.append(f"  warn  {f.where()}{f.message}")
        return "\n".join(rows)


def _key(f: Finding) -> tuple[str, str]:
    return (f.check, f.message)


def run_gate(
    path: str | Path,
    *,
    baseline: str | Path | Sequence[str | Path] | None = None,
    options: GateOptions | None = None,
    password: str | None = None,
) -> GateReport:
    path = Path(path)
    opts = options or GateOptions()
    found = checks.run_all(path, opts, password=password)
    report = GateReport(path, pngs=found.pngs)

    base_counts: Counter = Counter()
    if baseline is not None:
        bases: Iterable[str | Path] = [baseline] if isinstance(baseline, (str, Path)) else baseline
        base_opts = GateOptions(**{**opts.__dict__, "png_dir": None, "redact_regions": None, "standard": None})
        for b in bases:
            try:
                for f in checks.run_all(Path(b), base_opts, password=password).findings:
                    base_counts[_key(f)] += 1
            except Exception:
                pass

    for f in found.findings:
        k = _key(f)
        if f.severity == "warn":
            report.warnings.append(f)
        elif base_counts[k] > 0 and f.check not in checks.NEVER_INHERITED:
            base_counts[k] -= 1
            report.warnings.append(Finding(f.check, f"{f.message} (already in input)", f.page, "", "warn"))
        else:
            report.failures.append(f)
    return report
