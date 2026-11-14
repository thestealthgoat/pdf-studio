from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "corpus"))


@pytest.fixture(scope="session")
def corpus() -> dict[str, Path]:
    import build  # corpus/build.py

    return build.build()


@pytest.fixture
def work(tmp_path, corpus):
    """Copy corpus files into a temp dir so ops write next to a throwaway source."""

    def _copy(name: str) -> Path:
        dst = tmp_path / f"{name}.pdf"
        shutil.copy(corpus[name], dst)
        return dst

    return _copy


def has(exe: str) -> bool:
    return shutil.which(exe) is not None
