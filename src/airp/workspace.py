"""User-owned AIRP data locations, independent from shipped resources."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _default_root() -> Path:
    override = os.environ.get("AIRP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "airp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "airp"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "airp"


@dataclass(frozen=True)
class Workspace:
    """Stable root for mutable Studio, Project, Session and secret data."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @classmethod
    def default(cls) -> "Workspace":
        return cls(_default_root())

    @classmethod
    def from_root(cls, root: str | Path) -> "Workspace":
        return cls(Path(root))

    @property
    def library_root(self) -> Path:
        return self.root / "library"

    @property
    def providers_root(self) -> Path:
        return self.library_root / "providers"

    @property
    def agents_root(self) -> Path:
        return self.library_root / "agents"

    @property
    def graphs_root(self) -> Path:
        return self.library_root / "graphs"

    @property
    def worldbooks_root(self) -> Path:
        return self.library_root / "worldbooks"

    @property
    def regex_collections_root(self) -> Path:
        return self.library_root / "regex_collections"

    @property
    def projects_root(self) -> Path:
        return self.root / "projects"

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def secrets_path(self) -> Path:
        return self.root / "secrets.json"

    @property
    def runtime_root(self) -> Path:
        return self.root / "runtime"

    def ensure(self) -> "Workspace":
        for path in (
            self.providers_root,
            self.agents_root,
            self.graphs_root,
            self.worldbooks_root,
            self.regex_collections_root,
            self.projects_root,
            self.sessions_root,
            self.runtime_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self
