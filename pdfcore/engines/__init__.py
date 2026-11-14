"""Engine registry.

An *engine* is a thin adapter for one job (``render``, ``text``) over one
library. The public edition ships permissive engines only. Other packages
(e.g. the private ``pdf-studio-local``) add engines through the
``pdfcore.backends`` entry-point group: each entry point is a callable that
receives this module's :func:`register` and calls it.

Selection order for ``get(kind)``: explicit ``name`` argument, then the
``PDFCORE_<KIND>_ENGINE`` environment variable, then the builtin default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable

KINDS = ("render", "text")


@dataclass
class _Entry:
    name: str
    factory: Callable[[], Any]
    default: bool
    source: str


_REG: dict[str, dict[str, _Entry]] = {k: {} for k in KINDS}
_loaded = False


def register(kind: str, name: str, factory: Callable[[], Any], *, default: bool = False, source: str = "") -> None:
    if kind not in _REG:
        raise ValueError(f"unknown engine kind {kind!r}; expected one of {KINDS}")
    _REG[kind][name] = _Entry(name, factory, default, source)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for ep in entry_points(group="pdfcore.backends"):
        try:
            hook = ep.load()
            hook(register)
        except Exception as exc:  # a broken plugin must not break the toolkit
            import warnings

            warnings.warn(f"pdfcore backend plugin {ep.name!r} failed to load: {exc}", stacklevel=2)
    # Builtins are always present even if the package metadata is missing (e.g. running from a checkout).
    if not _REG["render"]:
        from pdfcore.engines import builtin

        builtin.register(register)


def available(kind: str) -> list[str]:
    _load()
    return sorted(_REG[kind])


_cache: dict[tuple[str, str], Any] = {}


def get(kind: str, name: str | None = None) -> Any:
    _load()
    entries = _REG[kind]
    name = name or os.environ.get(f"PDFCORE_{kind.upper()}_ENGINE")
    if not name:
        defaults = [e for e in entries.values() if e.default]
        if not defaults:
            raise RuntimeError(f"no default {kind} engine registered")
        name = defaults[0].name
    if name not in entries:
        raise KeyError(f"{kind} engine {name!r} not installed; available: {sorted(entries)}")
    key = (kind, name)
    if key not in _cache:
        _cache[key] = entries[name].factory()
    return _cache[key]
