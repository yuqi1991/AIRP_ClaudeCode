"""Provider credential storage isolated from Studio definitions and runtime records."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SecretStore(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    @abstractmethod
    def redact(self, value: Any) -> Any: ...


class LocalSecretStore(SecretStore):
    """Atomic, user-readable-only JSON secret store for local phase-one use."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()

    def get(self, key: str) -> str | None:
        self._validate_key(key)
        with self._lock:
            value = self._read().get(key)
        return value if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        self._validate_key(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("secret value must be a non-empty string")
        with self._lock:
            secrets = self._read()
            secrets[key] = value.strip()
            self._write(secrets)

    def delete(self, key: str) -> bool:
        self._validate_key(key)
        with self._lock:
            secrets = self._read()
            if key not in secrets:
                return False
            del secrets[key]
            self._write(secrets)
            return True

    def redact(self, value: Any) -> Any:
        with self._lock:
            secret_values = tuple(
                item for item in self._read().values() if isinstance(item, str) and item
            )

        def visit(item):
            if isinstance(item, dict):
                return {key: visit(nested) for key, nested in item.items()}
            if isinstance(item, list):
                return [visit(nested) for nested in item]
            if isinstance(item, tuple):
                return tuple(visit(nested) for nested in item)
            if isinstance(item, str):
                for secret in secret_values:
                    item = item.replace(secret, "[REDACTED]")
                return item
            return item

        return visit(value)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key or any(char in key for char in "\r\n\0"):
            raise ValueError("secret key must be a non-empty single-line string")

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read local secret store") from exc
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ValueError("local secret store must contain a string map")
        return raw

    def _write(self, secrets: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.stem}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(secrets, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
