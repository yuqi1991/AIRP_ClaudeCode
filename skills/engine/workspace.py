"""Compatibility import for the production Workspace module."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from airp.workspace import Workspace  # noqa: E402

__all__ = ["Workspace"]
