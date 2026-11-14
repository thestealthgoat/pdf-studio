"""``pdfcore recipe``: run a YAML recipe (batch workflow) over files. Library: :func:`recipe`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from pdfcore.ops import command
from pdfcore.recipes import Recipe, list_examples, load_recipe, run_recipe
from pdfcore.result import OpResult

__all__ = ["recipe"]


def recipe(
    spec: str | Path | Mapping[str, Any] | Recipe,
    inputs: Iterable[str | Path] | None = None,
    *,
    out_dir: str | Path | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> OpResult:
    """Run a recipe; ``output`` is the last file written, ``extra_outputs`` all of them, ``gate`` the first
    failing file's report (``None`` when every file passed). ``data['run']`` is the :class:`RecipeRun`."""
    run = run_recipe(spec, inputs, out_dir=out_dir, dry_run=dry_run, quiet=quiet)
    outs = [f.output for f in run.files if f.output is not None]
    extras = [p for f in run.files for p in ([f.output] if f.output else []) + f.extra] if not dry_run else []
    bad = next((f.gate for f in run.files if not f.ok and f.gate is not None), None)
    data = {
        "run": run,
        "recipe": run.recipe.name,
        "passed": run.passed,
        "failed": run.failed,
        "files": [{"input": str(f.input), "output": str(f.output) if f.output else None, "ok": f.ok, "line": f.line}
                  for f in run.files],
    }
    if dry_run and not run.ok and bad is None:
        from pdfcore.gate import GateReport
        from pdfcore.gate.checks import Finding

        probs = [f"step {s.index} {s.target}: {s.problem}" for s in run.plan if not s.available]
        bad = GateReport(Path(str(run.recipe.source or run.recipe.name)), failures=[Finding("recipe", probs[0])])
    return OpResult(outs[-1] if outs and not dry_run else None, bad, extras, data)


@command("recipe", "Run a YAML recipe (chain of ops) over files; --dry-run shows the plan", group="automate")
def _cli_recipe(p):
    p.add_argument("recipe", nargs="?", help="recipe YAML file or example name (see --list)")
    p.add_argument("inputs", nargs="*", help="files, folders or globs (default: the recipe's 'input')")
    p.add_argument("--dry-run", action="store_true", help="print the plan and check every op resolves")
    p.add_argument("--out-dir", help="write results here instead of the recipe's 'output' / next to each input")
    p.add_argument("--list", action="store_true", help="list the bundled example recipes")

    def run(ns):
        if ns.list or not ns.recipe:
            for ex in list_examples():
                r = load_recipe(ex)
                print(f"{ex.stem:22s} {r.description}")
            return None
        return recipe(ns.recipe, ns.inputs or None, out_dir=ns.out_dir, dry_run=ns.dry_run)

    return run
