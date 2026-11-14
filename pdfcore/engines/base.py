"""Engine protocols. Engines are stateless; they take a path and a page index."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image

from pdfcore.types import Char, PathItem, Word


@runtime_checkable
class RenderEngine(Protocol):
    name: str

    def page_count(self, path: Path, password: str | None = None) -> int: ...

    def render(self, path: Path, page: int, dpi: float = 150, password: str | None = None) -> Image.Image:
        """Rasterize one page to an RGB image."""
        ...


@runtime_checkable
class TextEngine(Protocol):
    name: str

    def chars(self, path: Path, page: int, password: str | None = None) -> list[Char]: ...

    def words(self, path: Path, page: int, password: str | None = None) -> list[Word]: ...

    def page_size(self, path: Path, page: int, password: str | None = None) -> tuple[float, float]: ...

    def paths(self, path: Path, page: int, password: str | None = None) -> list[PathItem]: ...
