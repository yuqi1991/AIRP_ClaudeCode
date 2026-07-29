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
        return repository_root() / "skills" / "styles"


def static_asset_root() -> Path:
    """Resolve the web asset source used to seed a runtime projection."""
    override = os.environ.get("AIRP_STATIC_ASSETS")
    if override:
        return Path(override).expanduser().resolve()
    packaged = packaged_web_root()
    if packaged.exists():
        return packaged
    return repository_root() / "skills" / "styles"


def projection_root(root: str | Path | None = None) -> Path:
    """Return a writable projection directory for the local runtime."""
    override = os.environ.get("AIRP_STATIC_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(root).expanduser().resolve() if root is not None else repository_root()
    legacy = base / "skills" / "styles"
    if legacy.exists():
        return legacy
    try:
        from airp.workspace import Workspace

        return Workspace.default().runtime_root / "styles"
    except Exception:
        return base / ".airp" / "web"


def sidecar_script() -> Path:
    """Return the provider sidecar script, honoring explicit overrides."""
    override = os.environ.get("AIRP_SIDECAR_SCRIPT")
    if override:
        return Path(override).expanduser().resolve()
    packaged = Path(files("airp.resources")) / "sidecar" / "pi_provider_sidecar.mjs"
    if packaged.exists():
        return packaged
    return repository_root() / "skills" / "sidecar" / "pi_provider_sidecar.mjs"


__all__ = [
    "packaged_web_root",
    "projection_root",
    "repository_root",
    "sidecar_script",
    "static_asset_root",
]
