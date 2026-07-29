"""Compatibility bootstrap for the transitional AIRP runtime.

The production package lives under :mod:`airp`, while the current runtime
implementation is still stored in ``skills/``.  This module is the single
place that knows how to locate that legacy tree.  Keeping the lookup here
lets the launcher migrate without scattering ``sys.path`` mutations through
application code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositoryLayout:
    """Filesystem layout used by the source checkout and compatibility code."""

    root: Path
    src: Path
    skills: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "RepositoryLayout":
        repository_root = Path(root).expanduser().resolve()
        return cls(
            root=repository_root,
            src=(repository_root / "src").resolve(),
            skills=(repository_root / "skills").resolve(),
        )

    @property
    def legacy_entrypoint(self) -> Path:
        return self.skills / "start_runtime.py"


def bootstrap_legacy_runtime(root: str | Path) -> RepositoryLayout:
    """Make the source package and legacy runtime importable, idempotently.

    ``skills/`` is intentionally a compatibility path.  New production code
    should import from ``airp``; this helper exists only while the runtime
    implementation is being moved behind that package boundary.
    """

    layout = RepositoryLayout.from_root(root)
    for path in (layout.src, layout.skills):
        path_string = str(path)
        if path_string not in sys.path:
            sys.path.insert(0, path_string)
    return layout
