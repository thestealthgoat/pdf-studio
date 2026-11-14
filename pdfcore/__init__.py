"""pdfcore — the engine behind PDF Studio.

Coordinates: unless a function says otherwise, rectangles are in PDF points
with a **top-left** origin: ``Rect(x0, top, x1, bottom)``. Page indexes are
0-based in code and 1-based on the command line and in reports.

Every operation writes a new file next to its source and never overwrites
the input. Each op returns an :class:`~pdfcore.result.OpResult` that carries
the output path and the quality-gate report.
"""

from __future__ import annotations

__version__ = "0.5.1"

from pdfcore.types import Rect  # noqa: E402
from pdfcore.result import OpResult  # noqa: E402

__all__ = ["Rect", "OpResult", "__version__"]
