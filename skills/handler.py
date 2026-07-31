#!/usr/bin/env python3
"""Compatibility alias for :mod:`airp.handler`.

The legacy ``handler`` import must resolve to the same module object as
``airp.handler``.  Re-exporting its functions creates a second patch point,
which breaks embedders that replace a handler function for projection retries.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

_handler = importlib.import_module("airp.handler")
sys.modules[__name__] = _handler


if __name__ == "__main__":  # pragma: no cover - legacy CLI compatibility
    _handler.main()
