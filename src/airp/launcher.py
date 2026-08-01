"""Installable entrypoint for the AIRP runtime."""

from __future__ import annotations

import sys
from typing import Sequence

from airp import cli


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
