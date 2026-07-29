"""Installable entrypoint for the AIRP runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from airp import cli


def load_legacy_launcher(root=None):
    """Return a compatibility-shaped view backed by the packaged CLI.

    ``root`` is retained for callers that used the old bootstrap helper.  It
    is metadata only; installed execution never imports a checkout script.
    """
    skills_root = Path(root).expanduser().resolve() / "skills" if root is not None else None
    return SimpleNamespace(__name__="start_runtime", SKILLS=skills_root, main=cli.main)


def main(argv: Sequence[str] | None = None) -> int | None:
    """Run the packaged runtime CLI."""

    if argv is None:
        return cli.main()

    original_argv = sys.argv
    sys.argv = [original_argv[0], *argv]
    try:
        return cli.main()
    finally:
        sys.argv = original_argv


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    main()
