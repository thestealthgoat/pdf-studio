"""Recipe runner (the Action Wizard equivalent).

A recipe is a YAML file::

    name: Bid set prep
    description: Rotate, footer, bookmarks
    input: incoming/*.pdf          # glob, folder, file, or a list of them (relative to the YAML file)
    output: out/                   # folder (relative to the YAML file); default: next to each input
    naming: "{stem}.bid.pdf"       # {stem} {name} {ext} {recipe} {date} {n}; default "{stem}.<recipe-slug>.pdf"
    gate: {profile: drawing}       # optional GateOptions for the final gate (size_budget → max_size_mb)
    steps:
      - {op: organize.rotate, pages: "2", angle: 90}
      - {op: stamp.header_footer, texts: {footer-center: "Page {page} of {pages}"}}

Each step names an op callable and passes the remaining keys as keyword arguments. Op names resolve
at run time, lazily: ``module.func`` → ``pdfcore.ops.module.func``; ``pkg.mod:func`` → any importable
function; a bare name → the alias table, then a search of the ``pdfcore.ops`` modules; names added
with :func:`register_op` win over everything. The step's input is passed positionally, ``quiet=True``
is passed when the callable accepts it, and ``res.output`` becomes the next step's input.

Conveniences: ``${VAR}`` in a string value is replaced from the environment (keep passwords out of
the file); hyphens in keys become underscores; a string ``pages`` spec ("1-3,5", "odd") is turned into
0-based indexes for ops whose ``pages`` parameter does not take a string; ``degrees``↔``angle``.

Each input is copied into a temporary folder; every step writes there; only the final PDF is
copied to the destination (never overwriting anything), then the quality gate runs on it against
the original input. Non-PDF step outputs (PNG pages, JSON, XLSX…) are copied next to it.
"""

from __future__ import annotations

import glob as _glob
import inspect
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from pdfcore.gate import GateOptions, GateReport, run_gate
from pdfcore.gate.checks import Finding
from pdfcore.result import OpResult

EXAMPLES_DIR = Path(__file__).with_name("examples")

# short name → "module.func" in pdfcore.ops (resolved lazily; missing modules are reported, not fatal)
ALIASES: dict[str, str] = {
    "merge": "organize.merge", "split": "organize.split", "rotate": "organize.rotate", "crop": "organize.crop",
    "insert": "organize.insert", "page_labels": "organize.page_labels",
    "stamp": "stamp.stamp", "watermark": "stamp.watermark", "header_footer": "stamp.header_footer",
    "footer": "stamp.header_footer", "bates": "stamp.stamp",
    "bookmarks_auto": "navigate.auto_bookmarks", "auto_bookmarks": "navigate.auto_bookmarks",
    "navigate.bookmarks_auto": "navigate.auto_bookmarks",
    "set_metadata": "navigate.set_metadata", "autolink": "navigate.autolink",
    "optimize": "optimize.optimize", "compress": "optimize.optimize",
    "encrypt": "security.encrypt", "decrypt": "security.decrypt",
    "ocr": "ocr.ocr", "redact": "redact.redact",
    "render": "extract.render", "text": "extract.text", "tables": "extract.tables", "vectors": "extract.vectors",
    "extract_images": "extract.images", "search": "extract.search",
}
# key synonyms tried when the op has no parameter with the key's own name
SYNONYMS: dict[str, tuple[str, ...]] = {
    "degrees": ("angle", "rotation"), "angle": ("degrees",), "rotation": ("angle", "degrees"),
    "output": ("out",),
}
RESERVED = {"op", "label"}

_REGISTRY: dict[str, Callable[..., Any]] = {}


class OpNotAvailable(LookupError):
    """The op name does not resolve (yet): unknown name or module not built/installed."""


def register_op(name: str, func: Callable[..., Any]) -> None:
    """Make *func* callable from recipes as *name* (overrides aliases and module lookup)."""
    _REGISTRY[name] = func


def unregister_op(name: str) -> None:
    _REGISTRY.pop(name, None)


def _import_ops_module(mod: str):
    try:
        return import_module(f"pdfcore.ops.{mod}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pdfcore.ops"):
            raise OpNotAvailable(f"module pdfcore.ops.{mod} is not available (not built yet?)") from exc
        raise OpNotAvailable(f"pdfcore.ops.{mod} needs a missing dependency: {exc.name}") from exc


def resolve_op(name: str) -> Callable[..., Any]:
    """Return the callable for an op name (see module docstring)."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    key = name.replace("-", "_")
    if key in _REGISTRY:
        return _REGISTRY[key]
    target = ALIASES.get(key, key)
    if ":" in target:
        mod_name, fn = target.split(":", 1)
        try:
            mod = import_module(mod_name)
        except ModuleNotFoundError as exc:
            raise OpNotAvailable(f"cannot import {mod_name}: {exc}") from exc
    elif "." in target:
        mod_name, fn = target.rsplit(".", 1)
        mod = _import_ops_module(mod_name)
    else:
        from pdfcore.ops import MODULES

        fn = target
        for m in MODULES:
            if m == "recipes_op":
                continue
            try:
                mod = _import_ops_module(m)
            except OpNotAvailable:
                continue
            if callable(getattr(mod, fn, None)) and not fn.startswith("_"):
                break
        else:
            raise OpNotAvailable(f"unknown op {name!r} (use module.function, e.g. organize.rotate)")
    func = getattr(mod, fn, None)
    if fn.startswith("_") or not callable(func):
        raise OpNotAvailable(f"{mod.__name__} has no op {fn!r}")
    return func


def op_label(name: str) -> str:
    key = name.replace("-", "_")
    return name if name in _REGISTRY or key in _REGISTRY else ALIASES.get(key, key)


# ---------------------------------------------------------------- model


@dataclass
class Step:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    label: str | None = None

    def describe(self) -> str:
        shown = []
        for k, v in self.args.items():
            v = "***" if "password" in k and v else v
            shown.append(f"{k}={v!r}")
        return f"{op_label(self.op)}({', '.join(shown)})"


@dataclass
class Recipe:
    name: str
    steps: list[Step]
    description: str = ""
    input: list[str] = field(default_factory=list)
    output: str | None = None
    naming: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)
    source: Path | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "recipe"

    @property
    def pattern(self) -> str:
        return self.naming or "{stem}.{recipe}.pdf"


_TOP_KEYS = {"name", "description", "input", "output", "naming", "gate", "steps"}


def list_examples() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.yaml"))


def load_recipe(spec: str | Path | Mapping[str, Any]) -> Recipe:
    """Load a recipe from a YAML path, an example name (``bid-set-prep``), or a dict."""
    source: Path | None = None
    if isinstance(spec, Mapping):
        raw: Any = dict(spec)
        base = Path.cwd()
    else:
        p = Path(spec)
        if not p.is_file():
            ex = EXAMPLES_DIR / (p.name if p.suffix in (".yaml", ".yml") else f"{p.name}.yaml")
            if p.parent == Path(".") and ex.is_file():
                p = ex
            else:
                names = ", ".join(x.stem for x in list_examples())
                raise FileNotFoundError(f"recipe {spec} not found (examples: {names})")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{p}: invalid YAML: {exc}") from exc
        source, base = p.resolve(), p.resolve().parent
    where = str(source) if source else "recipe"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping with 'name' and 'steps'")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"{where}: 'steps' must be a non-empty list")
    steps = []
    for k, s in enumerate(steps_raw, start=1):
        if isinstance(s, str):
            s = {"op": s}
        if not isinstance(s, dict) or not isinstance(s.get("op"), str):
            raise ValueError(f"{where}: step {k} needs an 'op' name")
        args = {str(key).replace("-", "_"): v for key, v in s.items() if key not in RESERVED}
        steps.append(Step(s["op"], args, s.get("label")))
    inp = raw.get("input") or []
    if isinstance(inp, str):
        inp = [inp]
    if not isinstance(inp, list):
        raise ValueError(f"{where}: 'input' must be a glob/folder/file or a list of them")
    gate = raw.get("gate") or {}
    if not isinstance(gate, dict):
        raise ValueError(f"{where}: 'gate' must be a mapping of gate options")
    name = str(raw.get("name") or (source.stem if source else "recipe"))
    r = Recipe(name, steps, str(raw.get("description") or ""), [str(x) for x in inp],
               str(raw["output"]) if raw.get("output") else None, raw.get("naming"), gate, base, source)
    _naming(r, Path("example.pdf"), 1)  # validate placeholders early
    return r


# ---------------------------------------------------------------- planning helpers


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(v: Any, missing: list[str]) -> Any:
    if isinstance(v, str):
        def sub(m: re.Match) -> str:
            val = os.environ.get(m.group(1))
            if val is None:
                missing.append(m.group(1))
                return m.group(0)
            return val

        return _ENV.sub(sub, v)
    if isinstance(v, list):
        return [_expand(x, missing) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x, missing) for k, x in v.items()}
    return v


def _params(func: Callable) -> tuple[dict[str, inspect.Parameter], bool]:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {}, True
    ps = dict(sig.parameters)
    varkw = any(p.kind is p.VAR_KEYWORD for p in ps.values())
    return ps, varkw


def _bind(func: Callable, args: dict[str, Any]) -> dict[str, Any]:
    """Map recipe keys to the op's parameters (synonyms), raising ValueError for unknown keys."""
    ps, varkw = _params(func)
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in ps or varkw and not any(s in ps for s in SYNONYMS.get(k, ())):
            out[k] = v
            continue
        for s in SYNONYMS.get(k, ()):
            if s in ps:
                out[s] = v
                break
        else:
            allowed = [n for n, p in ps.items() if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)][1:]
            raise ValueError(f"unknown parameter {k!r} (accepts: {', '.join(allowed)})")
    return out


def _adapt_pages(func: Callable, kwargs: dict[str, Any], current: Path) -> dict[str, Any]:
    """A string 'pages' spec for an op whose 'pages' parameter takes indexes → 0-based list."""
    v = kwargs.get("pages")
    if not isinstance(v, (str, int)):
        return kwargs
    ps, _ = _params(func)
    p = ps.get("pages")
    ann = p.annotation if p is not None else inspect.Parameter.empty
    ann_s = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    if p is None or ann is inspect.Parameter.empty or "str" in str(ann_s):
        if isinstance(v, int):
            return {**kwargs, "pages": str(v)}
        return kwargs
    from pdfcore import engines
    from pdfcore.ops._common import parse_pages

    n = engines.get("render").page_count(current, password=kwargs.get("password"))
    return {**kwargs, "pages": parse_pages(str(v), n)}


def _call(func: Callable, current: Path, kwargs: dict[str, Any]) -> Any:
    ps, varkw = _params(func)
    kw = dict(kwargs)
    if "quiet" in ps or varkw:
        kw.setdefault("quiet", True)
    return func(current, **kw)


def _collect_inputs(specs: Iterable[str], base: Path, recipe: Recipe) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    own = re.compile(rf"\.{re.escape(recipe.slug)}(-\d+)?$")
    for s in specs:
        p = Path(os.path.expanduser(s))
        if not p.is_absolute():
            p = base / p
        if p.is_dir():
            found = sorted(p.glob("*.pdf")) + sorted(p.glob("*.PDF"))
            filt = True
        elif p.is_file():
            found, filt = [p], False
        else:
            found = sorted(Path(x) for x in _glob.glob(str(p), recursive=True) if Path(x).is_file())
            filt = True
            if not found:
                raise FileNotFoundError(f"no files match {s}")
        for f in found:
            f = f.resolve()
            if filt and own.search(f.stem):
                continue  # an earlier output of this recipe
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _naming(recipe: Recipe, src: Path, n: int) -> str:
    vals = {"stem": src.stem, "name": src.name, "ext": src.suffix, "recipe": recipe.slug,
            "date": date.today().isoformat(), "n": n}
    try:
        name = recipe.pattern.format(**vals)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"naming {recipe.pattern!r}: bad placeholder {exc} (use {{stem}} {{name}} {{ext}} {{recipe}} {{date}} {{n}})") from exc
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if "/" in name or "\\" in name:
        raise ValueError(f"naming {recipe.pattern!r} must produce a file name, not a path")
    return name


def _unique(p: Path, avoid: Path | None = None) -> Path:
    cand, k = p, 2
    while cand.exists() or (avoid is not None and cand.resolve() == avoid.resolve()):
        cand = p.with_name(f"{p.stem}-{k}{p.suffix}")
        k += 1
    return cand


def _dest_dir(recipe: Recipe, src: Path, out_dir: str | Path | None) -> Path:
    if out_dir is not None:
        return Path(out_dir).expanduser().resolve()
    if recipe.output:
        d = Path(os.path.expanduser(recipe.output))
        return d if d.is_absolute() else (recipe.base_dir / d)
    return src.parent


def _gate_options(recipe: Recipe) -> GateOptions:
    o = GateOptions()
    for k, v in recipe.gate.items():
        k = {"size_budget": "max_size_mb", "allow_blank": "allow_blank_pages"}.get(k.replace("-", "_"), k.replace("-", "_"))
        if not hasattr(o, k):
            raise ValueError(f"unknown gate option {k!r}")
        setattr(o, k, Path(v) if k == "png_dir" and v else v)
    return o


# ---------------------------------------------------------------- results


@dataclass
class StepPlan:
    index: int
    step: Step
    target: str
    available: bool
    problem: str = ""


@dataclass
class FileOutcome:
    input: Path
    output: Path | None
    ok: bool
    line: str
    gate: GateReport | None = None
    extra: list[Path] = field(default_factory=list)
    steps_done: int = 0


@dataclass
class RecipeRun:
    recipe: Recipe
    plan: list[StepPlan]
    files: list[FileOutcome]
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.files) and (not self.dry_run or all(s.available for s in self.plan))

    @property
    def passed(self) -> int:
        return sum(1 for f in self.files if f.ok)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if not f.ok)

    def summary(self) -> str:
        what = "planned" if self.dry_run else "processed"
        return f"recipe '{self.recipe.name}': {len(self.files)} file(s) {what}, {self.passed} PASS, {self.failed} FAIL"


def plan(recipe: Recipe) -> list[StepPlan]:
    out = []
    for k, st in enumerate(recipe.steps, start=1):
        target = op_label(st.op)
        try:
            func = resolve_op(st.op)
            missing: list[str] = []
            args = _expand(st.args, missing)
            _bind(func, args)
            problem = f"environment variable(s) not set: {', '.join(sorted(set(missing)))}" if missing else ""
            out.append(StepPlan(k, st, getattr(func, "__module__", "?").replace("pdfcore.ops.", "") + "." +
                                getattr(func, "__name__", target), not missing, problem))
        except OpNotAvailable as exc:
            out.append(StepPlan(k, st, target, False, str(exc)))
        except ValueError as exc:
            out.append(StepPlan(k, st, target, False, str(exc)))
    return out


def _fail(path: Path, msg: str) -> GateReport:
    return GateReport(path, failures=[Finding("recipe", msg)])


def _is_pdf(p: Any) -> bool:
    return isinstance(p, (str, Path)) and Path(p).suffix.lower() == ".pdf" and Path(p).is_file()


def _copy_side(f: Path, tmp: Path, dest_dir: Path) -> Path | None:
    f = Path(f)
    if not f.exists():
        return None
    try:
        rel = f.resolve().relative_to(tmp.resolve())
    except ValueError:
        return f  # written outside the work folder (e.g. the op was given out=…): leave it there
    target = dest_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if f.is_dir():
        target = _unique(target)
        shutil.copytree(f, target)
    else:
        target = _unique(target)
        shutil.copy2(f, target)
    return target


def _run_one(recipe: Recipe, src: Path, n: int, out_dir: str | Path | None, quiet: bool) -> FileOutcome:
    dest_dir = _dest_dir(recipe, src, out_dir)
    final_name = _naming(recipe, src, n)
    gate_pw: str | None = None
    extras: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="pdfcore-recipe-") as tmpd:
        tmp = Path(tmpd)
        work = tmp / src.name
        shutil.copy2(src, work)
        current = work
        side: list[Path] = []
        for k, st in enumerate(recipe.steps, start=1):
            label = op_label(st.op)
            try:
                func = resolve_op(st.op)
                missing: list[str] = []
                args = _expand(st.args, missing)
                if missing:
                    raise ValueError(f"environment variable(s) not set: {', '.join(sorted(set(missing)))}")
                kwargs = _adapt_pages(func, _bind(func, args), current)
                res = _call(func, current, kwargs)
            except Exception as exc:  # one bad file must not stop the batch
                msg = f"step {k} {label}: {exc}"
                return FileOutcome(src, None, False, f"FAIL {msg}", _fail(src, msg), extras, k - 1)
            output = res.output if isinstance(res, OpResult) else res if isinstance(res, (str, Path)) else None
            if isinstance(res, OpResult):
                if not res.ok:
                    line = res.line()
                    msg = f"step {k} {label}: {line}"
                    return FileOutcome(src, None, False, f"FAIL {msg}", res.gate or _fail(src, msg), extras, k - 1)
                side += [Path(p) for p in res.extra_outputs if not (_is_pdf(p) and Path(p) == Path(output or ""))]
            if _is_pdf(output):
                current = Path(output)
                if label.endswith("encrypt") or getattr(func, "__name__", "") == "encrypt":
                    gate_pw = kwargs.get("user_password") or None
            elif output is not None:
                side.append(Path(output))
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in dict.fromkeys(side):
            c = _copy_side(f, tmp, dest_dir)
            if c is not None:
                extras.append(c)
        if current == work:
            msg = "no step produced a PDF"
            ok = bool(extras)
            line = "PASS (side outputs only)" if ok else f"FAIL {msg}"
            return FileOutcome(src, extras[0] if extras else None, ok, line, None if ok else _fail(src, msg), extras,
                               len(recipe.steps))
        dest = _unique(dest_dir / final_name, avoid=src)
        shutil.copy2(current, dest)
    rep = run_gate(dest, baseline=src, options=_gate_options(recipe), password=gate_pw)
    return FileOutcome(src, dest, rep.passed, rep.line(), rep, extras, len(recipe.steps))


def run_recipe(
    recipe: str | Path | Mapping[str, Any] | Recipe,
    inputs: Iterable[str | Path] | None = None,
    *,
    out_dir: str | Path | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> RecipeRun:
    """Run *recipe* on *inputs* (default: the recipe's ``input``). Prints one line per file and a summary."""
    r = recipe if isinstance(recipe, Recipe) else load_recipe(recipe)
    if inputs is not None:
        files = _collect_inputs([str(x) for x in inputs], Path.cwd(), r)
    elif r.input:
        files = _collect_inputs(r.input, r.base_dir, r)
    else:
        raise ValueError(f"recipe '{r.name}' has no 'input'; pass the files to process")
    say = (lambda *a: None) if quiet else print
    steps = plan(r)
    if dry_run:
        say(f"Recipe: {r.name}" + (f" — {r.description}" if r.description else ""))
        say(f"Steps ({len(steps)}):")
        for sp in steps:
            mark = "ok" if sp.available else f"NOT AVAILABLE: {sp.problem}"
            say(f"  {sp.index}. {sp.step.describe()}  [{mark}]")
        say(f"Files ({len(files)}):")
        outs = []
        for n, f in enumerate(files, start=1):
            dest = _unique(_dest_dir(r, f, out_dir) / _naming(r, f, n), avoid=f)
            ok = all(s.available for s in steps)
            outs.append(FileOutcome(f, dest, ok, "PLANNED" if ok else "BLOCKED"))
            say(f"  {f}  →  {dest}")
        run = RecipeRun(r, steps, outs, dry_run=True)
        say(("plan OK" if run.ok else "plan has unavailable steps") + f": {len(files)} file(s), {len(steps)} step(s)")
        return run
    if not files:
        raise FileNotFoundError(f"recipe '{r.name}': no input files")
    outcomes = []
    for n, f in enumerate(files, start=1):
        oc = _run_one(r, f, n, out_dir, quiet)
        outcomes.append(oc)
        say(f"{oc.output or oc.input}  {oc.line}")
    run = RecipeRun(r, steps, outcomes)
    say(run.summary())
    return run
