#!/usr/bin/env python3
"""Compatibility wrapper for :mod:`airp.import_card`."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from airp.import_card import *  # noqa: F401,F403,E402


if __name__ == "__main__":  # pragma: no cover - legacy CLI compatibility
    from airp.import_card import main

    main()
