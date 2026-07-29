"""Installable entrypoint for the transitional local AIRP runtime."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Sequence

from airp.bootstrap import RepositoryLayout, bootstrap_legacy_runtime


def _repository_root() -> Path:
    override = os.environ.get("AIRP_REPOSITORY_ROOT")
    if override:
        return Path(override).expanduser()
    # ``src/airp/launcher.py`` in a source checkout.
    return Path(__file__).resolve().parents[2]


def load_legacy_launcher(root: str | Path | None = None):
    """Return the legacy launcher module after applying the compatibility seam."""

    layout = bootstrap_legacy_runtime(root or _repository_root())
    if not layout.legacy_entrypoint.is_file():
        raise RuntimeError(
            "AIRP runtime source tree is unavailable; set AIRP_REPOSITORY_ROOT "
            "to a checkout containing skills/start_runtime.py"
        )
    return importlib.import_module("start_runtime")


def main(argv: Sequence[str] | None = None) -> int | None:
    """Delegate to ``skills/start_runtime.py`` without duplicating behavior."""

    launcher = load_legacy_launcher()
    if argv is None:
        return launcher.main()

    original_argv = sys.argv
    sys.argv = [original_argv[0], *argv]
    try:
        return launcher.main()
    finally:
        sys.argv = original_argv


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    main()
