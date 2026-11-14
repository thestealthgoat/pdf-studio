"""Plain data types shared across engines and ops."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned box in points, top-left origin."""

    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def intersects(self, other: "Rect") -> bool:
        return not (
            other.x0 >= self.x1 or other.x1 <= self.x0 or other.top >= self.bottom or other.bottom <= self.top
        )

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.top <= y <= self.bottom

    def overlap_fraction(self, other: "Rect") -> float:
        """Fraction of *other*'s area that lies inside self."""
        ix0, it = max(self.x0, other.x0), max(self.top, other.top)
        ix1, ib = min(self.x1, other.x1), min(self.bottom, other.bottom)
        if ix1 <= ix0 or ib <= it or other.area == 0:
            return 0.0
        return (ix1 - ix0) * (ib - it) / other.area

    def expand(self, d: float) -> "Rect":
        return Rect(self.x0 - d, self.top - d, self.x1 + d, self.bottom + d)

    def to_pdf(self, page_height: float) -> tuple[float, float, float, float]:
        """Return (llx, lly, urx, ury) in PDF user space (bottom-left origin)."""
        return (self.x0, page_height - self.bottom, self.x1, page_height - self.top)

    @classmethod
    def from_pdf(cls, llx: float, lly: float, urx: float, ury: float, page_height: float) -> "Rect":
        x0, x1 = sorted((llx, urx))
        y0, y1 = sorted((lly, ury))
        return cls(x0, page_height - y1, x1, page_height - y0)

    @classmethod
    def union(cls, rects: Iterable["Rect"]) -> "Rect":
        rs = list(rects)
        return cls(min(r.x0 for r in rs), min(r.top for r in rs), max(r.x1 for r in rs), max(r.bottom for r in rs))


@dataclass(slots=True)
class Char:
    text: str
    rect: Rect
    font: str = ""
    size: float = 0.0


@dataclass(slots=True)
class Word:
    text: str
    rect: Rect
    page: int
    font: str = ""
    size: float = 0.0
    line_no: int = 0
    block_no: int = 0


@dataclass(slots=True)
class Line:
    words: list[Word]
    page: int
    block_no: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def rect(self) -> Rect:
        return Rect.union(w.rect for w in self.words)


@dataclass(slots=True)
class Block:
    lines: list[Line]
    page: int

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def rect(self) -> Rect:
        return Rect.union(line.rect for line in self.lines)


@dataclass(slots=True)
class Hit:
    """A search match: the matched text and one rectangle per line it spans."""

    text: str
    page: int
    rects: list[Rect] = field(default_factory=list)


@dataclass(slots=True)
class PathItem:
    """A vector drawing primitive extracted from a page."""

    kind: str  # "line" | "rect" | "curve"
    points: list[tuple[float, float]]  # top-left origin
    stroke: tuple[float, ...] | None
    fill: tuple[float, ...] | None
    width: float
    page: int

    @property
    def rect(self) -> Rect:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return Rect(min(xs), min(ys), max(xs), max(ys))
