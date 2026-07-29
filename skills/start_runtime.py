#!/usr/bin/env python3
"""Compatibility wrapper for the installable :mod:`airp.cli` entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

SKILLS = Path(__file__).resolve().parent

from airp.cli import *  # noqa: F401,F403,E402
from airp.cli import _deliver_opening, main


if __name__ == "__main__":  # pragma: no cover - legacy CLI compatibility
    main()
