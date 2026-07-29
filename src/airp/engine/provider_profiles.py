"""Studio Provider Profile lifecycle, including secrets and connection operations."""

from __future__ import annotations

from typing import Any, Callable

from airp.engine.provider import OpenAICompatibleProviderAdapter, ProviderError


class ProviderConnectionError(RuntimeError):
    def __init__(self, error: ProviderError, secret_store) -> None:
        super().__init__(secret_store.redact(error.message))
        self.category = error.category
        self.retryable = error.retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "category": self.category,
                "retryable": self.retryable,
                "message": str(self),
            },
        }


class ProviderProfileService:
    """Deep module for safe Provider Profile persistence and execution setup."""

    def __init__(self, profile_store, secret_store, *, discovery_timeout: float = 10.0):
        self._profiles = profile_store
        self._secrets = secret_store
        self._discovery_timeout = discovery_timeout

    def list_profiles(self) -> list[dict[str, Any]]:
        return [self._safe(profile) for profile in self._profiles.list_profiles()]

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return self._safe(self._profiles.get_profile(profile_id))

    def create_profile(self, payload: dict) -> dict[str, Any]:
        return self._save(payload, self._profiles.create_profile)

    def update_profile(self, profile_id: str, payload: dict) -> dict[str, Any]:
        return self._save(payload, lambda clean: self._profiles.update_profile(profile_id, clean))

    def delete_profile(self, profile_id: str) -> None:
        self._profiles.delete_profile(profile_id)
        self._secrets.delete(profile_id)

    def test_connection(self, profile_id: str) -> list[str]:
        profile = self._profiles.get_profile(profile_id)
        try:
            return self._adapter(profile).test_connection()
        except ProviderError as exc:
            raise ProviderConnectionError(exc, self._secrets) from exc

    def refresh_models(self, profile_id: str) -> dict[str, Any]:
        profile = self._profiles.get_profile(profile_id)
        try:
            models = self._adapter(profile).discover_models()
        except ProviderError as exc:
            raise ProviderConnectionError(exc, self._secrets) from exc
        return self._safe(self._profiles.update_profile(profile_id, {"model_ids": models}))

    def delete_secret(self, profile_id: str) -> dict[str, Any]:
        profile = self._profiles.get_profile(profile_id)
        self._secrets.delete(profile_id)
        return self._safe(profile)

    def execution_adapter(self, profile_id: str, model: str) -> OpenAICompatibleProviderAdapter:
        profile = self._profiles.get_profile(profile_id)
        if not profile.get("enabled", True):
            raise ValueError(f"Provider Profile {profile_id!r} is disabled")
        return self._adapter(profile, model=model)

    def _save(self, payload: dict, persist: Callable[[dict], dict]) -> dict[str, Any]:
        clean, api_key = self._split_secret(payload)
        profile = persist(clean)
        if api_key is not None:
            self._secrets.set(profile["id"], api_key)
        try:
            models = self._adapter(profile).discover_models()
        except ProviderError as exc:
            discovery = ProviderConnectionError(exc, self._secrets).to_dict()
        else:
            profile = self._profiles.update_profile(profile["id"], {"model_ids": models})
            discovery = {"ok": True, "model_ids": models}
        return {"profile": self._safe(profile), "model_discovery": discovery}

    @staticmethod
    def _split_secret(payload: dict) -> tuple[dict, str | None]:
        if not isinstance(payload, dict):
            return payload, None
        clean = dict(payload)
        api_key = clean.pop("api_key", None)
        clean.pop("apiKey", None)
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise ValueError("api_key must be a non-empty string")
        return clean, api_key.strip() if api_key is not None else None

    def _safe(self, profile: dict) -> dict[str, Any]:
        return {**profile, "key_configured": self._secrets.has(profile["id"])}

    def _adapter(self, profile: dict, *, model: str | None = None) -> OpenAICompatibleProviderAdapter:
        api_key = self._secrets.get(profile["id"])
        if not api_key:
            raise ProviderError("API key is not configured", "provider_rejected", False)
        return OpenAICompatibleProviderAdapter(
            base_url=profile["base_url"],
            api_key=api_key,
            api_format=profile["api_format"],
            model=model or (profile.get("model_ids") or [""])[0],
            timeout=self._discovery_timeout,
        )
