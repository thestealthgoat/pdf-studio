"""Exceptions raised by the native DWG reader."""

from __future__ import annotations


class DwgReadError(Exception):
    """The file is not a DWG this reader can parse (bad signature, truncated or corrupt data,
    unsupported version or encryption). The message says which structure failed."""
