"""Bundled AIRP runtime resources and path resolution."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path


def repository_root() -> Path:
    """Return the source checkout root when one is available."""
    override = os.environ.get("AIRP_REPOSITORY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def packaged_web_root() -> Path:
    """Return the immutable web asset directory bundled with the package."""
    try:
        return Path(files("airp.web"))
    except ModuleNotFoundError:
        return Path(__file__).resolve().parents[1] / "web"


def static_asset_root() -> Path:
    """Resolve the web asset source used to seed a runtime projection."""
    override = os.environ.get("AIRP_STATIC_ASSETS")
    if override:
        return Path(override).expanduser().resolve()
    packaged = packaged_web_root()
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[1] / "web"


def projection_root(root: str | Path | None = None) -> Path:
    """Return a writable projection directory for the local runtime."""
    override = os.environ.get("AIRP_STATIC_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(root).expanduser().resolve() if root is not None else None
    try:
        from airp.workspace import Workspace

        workspace = Workspace.from_root(base) if base is not None else Workspace.default()
        return workspace.runtime_root / "styles"
    except Exception:
        return (base or repository_root()) / ".airp" / "web"


__all__ = [
    "packaged_web_root",
    "projection_root",
    "repository_root",
    "static_asset_root",
]
