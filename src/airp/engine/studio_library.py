"""Persistent Studio library objects.

The library is deliberately separate from legacy runtime settings files.
Provider Profiles are reusable definitions. Historic ``settings.json`` files
are read only by the one-time Studio migration path, never by active runs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SUPPORTED_API_FORMATS = frozenset({"responses", "chat_completions"})


class ProviderProfileError(ValueError):
    """A user-facing Provider Profile validation or persistence failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        references: list[dict[str, str]] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.references = references or []

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": str(self),
        }
        if self.references:
            payload["references"] = self.references
        return payload


class ProviderProfileStore:
    """File-backed CRUD store for reusable Provider Profile definitions.

    The store owns only non-secret provider metadata. API keys are rejected at
    this boundary until the SecretStore contract is available in ticket 02.
    Agent files are inspected only to provide dependency-protected deletion;
    no Agent model is imported here.
    """

    def __init__(
        self,
        static_root: str | Path,
        *,
        library_root: str | Path | None = None,
        workspace=None,
    ):
        self.static_root = Path(static_root).resolve()
        workspace_providers_root = getattr(workspace, "providers_root", None)
        workspace_agents_root = getattr(workspace, "agents_root", None)
        self.library_root = (
            Path(library_root).resolve()
            if library_root is not None
            else (Path(workspace_providers_root).resolve() if workspace_providers_root else self.static_root / "studio" / "providers")
        )
        self._agent_roots = (
            (Path(workspace_agents_root).resolve() if workspace_agents_root else self.static_root / "studio" / "agents"),
            self.static_root / "agents",
        )
        self._lock = threading.RLock()

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            profiles = [self._read_profile(path) for path in self._paths()]
            return sorted(profiles, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        path = self._path_for(profile_id)
        with self._lock:
            if not path.is_file():
                raise ProviderProfileError(
                    "provider_profile_not_found",
                    f"Provider Profile {profile_id!r} was not found",
                    status=404,
                )
            return self._read_profile(path)

    def create_profile(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProviderProfileError("invalid_provider_profile", "Provider Profile must be an object")
        with self._lock:
            profile_id = payload.get("id") or f"provider-{uuid.uuid4().hex[:12]}"
            self._validate_id(profile_id)
            path = self._path_for(profile_id)
            if path.exists():
                raise ProviderProfileError(
                    "provider_profile_exists",
                    f"Provider Profile {profile_id!r} already exists",
                    status=409,
                )
            profile = self._normalize(payload, profile_id=profile_id)
            now = int(time.time())
            profile["created_at"] = now
            profile["updated_at"] = now
            self._write_profile(path, profile)
            return profile

    def update_profile(self, profile_id: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProviderProfileError("invalid_provider_profile", "Provider Profile must be an object")
        with self._lock:
            path = self._path_for(profile_id)
            if not path.is_file():
                raise ProviderProfileError(
                    "provider_profile_not_found",
                    f"Provider Profile {profile_id!r} was not found",
                    status=404,
                )
            current = self._read_profile(path)
            merged = {**current, **payload}
            if "api_format" not in payload and "protocol" in payload:
                merged["api_format"] = payload["protocol"]
            if "api_format" not in payload and "format" in payload:
                merged["api_format"] = payload["format"]
            if "model_ids" not in payload and "models" in payload:
                merged["model_ids"] = payload["models"]
            if "model_ids" not in payload and "model_catalog" in payload:
                merged["model_ids"] = payload["model_catalog"]
            merged["id"] = profile_id
            profile = self._normalize(merged, profile_id=profile_id)
            profile["created_at"] = current.get("created_at", 0)
            profile["updated_at"] = int(time.time())
            self._write_profile(path, profile)
            return profile

    def set_enabled(self, profile_id: str, enabled: bool) -> dict[str, Any]:
        return self.update_profile(profile_id, {"enabled": enabled})

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            path = self._path_for(profile_id)
            if not path.is_file():
                raise ProviderProfileError(
                    "provider_profile_not_found",
                    f"Provider Profile {profile_id!r} was not found",
                    status=404,
                )
            references = self._references_for(profile_id)
            if references:
                raise ProviderProfileError(
                    "provider_profile_in_use",
                    f"Provider Profile {profile_id!r} is still referenced",
                    status=409,
                    references=references,
                )
            path.unlink()

    def _normalize(self, payload: dict[str, Any], *, profile_id: str) -> dict[str, Any]:
        self._validate_id(profile_id)
        if any(key in payload for key in ("api_key", "apiKey", "secret", "secret_ref")):
            raise ProviderProfileError(
                "secret_not_supported",
                "API keys are managed by the SecretStore and cannot be saved in a Provider Profile",
            )

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProviderProfileError("invalid_provider_profile", "name must be a non-empty string")

        base_url = payload.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ProviderProfileError("invalid_provider_profile", "base_url must be a non-empty string")
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ProviderProfileError("invalid_provider_profile", "base_url must use http:// or https://")

        api_format = payload.get(
            "api_format",
            payload.get("protocol", payload.get("format", "chat_completions")),
        )
        if not isinstance(api_format, str) or api_format not in SUPPORTED_API_FORMATS:
            raise ProviderProfileError(
                "invalid_provider_profile",
                "api_format must be responses or chat_completions",
            )
        if "api_format" in payload and "protocol" in payload and payload["api_format"] != payload["protocol"]:
            raise ProviderProfileError("invalid_provider_profile", "api_format and protocol must agree")
        if "api_format" in payload and "format" in payload and payload["api_format"] != payload["format"]:
            raise ProviderProfileError("invalid_provider_profile", "api_format and format must agree")
        if "protocol" in payload and "format" in payload and payload["protocol"] != payload["format"]:
            raise ProviderProfileError("invalid_provider_profile", "protocol and format must agree")

        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProviderProfileError("invalid_provider_profile", "enabled must be a boolean")

        raw_models = payload.get("model_ids", payload.get("models", payload.get("model_catalog", [])))
        if "model_ids" in payload and "models" in payload and payload["model_ids"] != payload["models"]:
            raise ProviderProfileError("invalid_provider_profile", "model_ids and models must agree")
        if not isinstance(raw_models, list):
            raise ProviderProfileError("invalid_provider_profile", "model_ids must be an array")
        model_ids: list[str] = []
        for model_id in raw_models:
            if not isinstance(model_id, str):
                raise ProviderProfileError("invalid_provider_profile", "model_ids must contain strings")
            model_id = model_id.strip()
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)

        return {
            "id": profile_id,
            "name": name.strip(),
            "base_url": base_url,
            "api_format": api_format,
            "enabled": enabled,
            "model_ids": model_ids,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }

    def _references_for(self, profile_id: str) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []
        paths: list[Path] = []
        for root in self._agent_roots:
            if root.is_dir():
                paths.extend(root.rglob("*.json"))
        paths.extend(
            path
            for path in (
                self.static_root / "studio" / "agents.json",
                self.static_root / "agents.json",
            )
            if path.is_file()
        )
        for path in sorted(set(paths)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict) and isinstance(data.get("agents"), list):
                candidates = data["agents"]
            else:
                candidates = [data]
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("provider_profile_id") != profile_id:
                    continue
                reference_id = candidate.get("id") if isinstance(candidate.get("id"), str) else path.stem
                name = candidate.get("name") if isinstance(candidate.get("name"), str) else reference_id
                references.append({"type": "agent", "id": reference_id, "name": name})
        return references

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _paths(self) -> list[Path]:
        if not self.library_root.is_dir():
            return []
        return [path for path in self.library_root.glob("*.json") if path.is_file()]

    def _path_for(self, profile_id: str) -> Path:
        self._validate_id(profile_id)
        self.library_root.mkdir(parents=True, exist_ok=True)
        path = (self.library_root / f"{profile_id}.json").resolve()
        try:
            path.relative_to(self.library_root)
        except ValueError as exc:
            raise ProviderProfileError("invalid_provider_profile", "invalid Provider Profile id") from exc
        return path

    def _validate_id(self, profile_id: Any) -> None:
        if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
            raise ProviderProfileError("invalid_provider_profile", "id must be a safe library identifier")

    def _read_profile(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderProfileError(
                "invalid_provider_profile",
                f"cannot read Provider Profile {path.stem!r}",
            ) from exc
        if not isinstance(raw, dict):
            raise ProviderProfileError("invalid_provider_profile", f"Provider Profile {path.stem!r} is not an object")
        return self._normalize(raw, profile_id=path.stem)

    def _write_profile(self, path: Path, profile: dict[str, Any]) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.library_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(profile, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


# Compatibility exports for callers that treat ``studio_library`` as the
# aggregate Studio library module. The implementation lives in its own deep
# module so Agent and Provider lifecycle concerns remain separate.
from airp.engine.agent_definitions import (  # noqa: E402
    AgentDefinitionError,
    AgentDefinitionService,
    AgentDefinitionStore,
)
from airp.engine.graph_definitions import (  # noqa: E402
    GraphDefinitionError,
    GraphDefinitionService,
    GraphDefinitionStore,
)
