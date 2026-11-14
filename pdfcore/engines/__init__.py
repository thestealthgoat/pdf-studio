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

KINDS = ("render", "text", "dwg")


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


_NO_DWG = (
    "No DWG converter installed. Options: install the free ODA File Converter "
    "(opendesign.com/guestfiles/oda_file_converter — accept its terms yourself), or the private "
    "local edition (LibreDWG). Or export DXF from your CAD program and use dxf2pdf."
)


def _external_dwg():
    """First installed non-native DWG→DXF converter: ODA, then any other registered (e.g. LibreDWG)."""
    order = sorted((n for n in _REG["dwg"] if n != "native"), key=lambda n: (n != "oda", n))
    for n in order:
        conv = get("dwg", n)
        if getattr(conv, "installed", lambda: True)():
            return conv
    return None


class _AutoDwg:
    """Per-file choice: the native reader when it supports the file's version (peeks the 6-byte version
    string), else — or if the native read fails — the first installed external converter."""

    name = "auto"

    def installed(self) -> bool:
        return True

    def to_dxf(self, src, out_dir):
        from pathlib import Path

        native = get("dwg", "native") if "native" in _REG["dwg"] else None
        if native is not None and native.can_read(Path(src)):
            try:
                return native.to_dxf(src, out_dir)
            except Exception as exc:  # corrupt/unsupported content: try an external converter if any
                ext = _external_dwg()
                if ext is None:
                    raise RuntimeError(f"native DWG reader failed: {exc}") from exc
                return ext.to_dxf(src, out_dir)
        ext = _external_dwg()
        if ext is None:
            raise RuntimeError("This DWG version is not supported by the built-in reader. " + _NO_DWG)
        return ext.to_dxf(src, out_dir)


def dwg_converter(src=None):
    """DWG→DXF converter. ``$PDFCORE_DWG_ENGINE`` wins; otherwise the built-in ``native`` reader when it
    can read ``src``'s version, falling back to ODA (if installed) or any other registered converter (e.g.
    LibreDWG from the local edition). Without ``src`` an auto-selecting converter is returned that makes
    that choice per file in ``to_dxf``. Raises with install advice if nothing can convert the file."""
    _load()
    name = os.environ.get("PDFCORE_DWG_ENGINE")
    if name:
        return get("dwg", name)
    if src is None:
        if "native" in _REG["dwg"]:
            return _AutoDwg()
    else:
        from pathlib import Path

        if "native" in _REG["dwg"] and get("dwg", "native").can_read(Path(src)):
            return get("dwg", "native")
    ext = _external_dwg()
    if ext is not None:
        return ext
    raise RuntimeError(_NO_DWG)
