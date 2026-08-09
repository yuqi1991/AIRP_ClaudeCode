"""Small persistence helpers for Studio Project/Library revisions."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


def revision(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def expected_revision(payload: Any) -> int | None:
    if not isinstance(payload, dict) or "expected_revision" not in payload:
        return None
    value = payload.get("expected_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else -1


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def changed_paths(before: Any, after: Any) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["$"] if before != after else []
    return sorted({key for key in set(before) | set(after) if before.get(key) != after.get(key)})


def append_audit(
    root: str | Path,
    *,
    object_type: str,
    object_id: str,
    parent_revision: int,
    new_revision: int,
    before: Any,
    after: Any,
    source: str = "api",
) -> dict[str, Any]:
    """Append a redacted, hash-based audit record and return it."""
    record = {
        "schema": "airp.config-audit",
        "version": 1,
        "object_type": object_type,
        "object_id": object_id,
        "parent_revision": parent_revision,
        "revision": new_revision,
        "timestamp": int(time.time()),
        "source": source if source in {"ui", "api", "import", "migration", "restore"} else "api",
        "changed_paths": changed_paths(before, after),
        "before_hash": canonical_hash(before),
        "after_hash": canonical_hash(after),
    }
    audit_root = Path(root)
    audit_root.mkdir(parents=True, exist_ok=True)
    with (audit_root / ".audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def conflict_payload(
    object_type: str,
    object_id: str,
    expected: int,
    current: int,
    current_object: dict[str, Any],
) -> dict[str, Any]:
    prefixes = {
        "provider_profile": "/v1/studio/providers/",
        "agent": "/v1/studio/agents/",
        "graph": "/v1/studio/graphs/",
        "regex_collection": "/v1/studio/regex-collections/",
        "project": "/v1/studio/projects/",
        "project_worldbooks": "/v1/studio/projects/",
        "worldbook": "/v1/studio/worldbooks/",
    }
    return {
        "object_type": object_type,
        "object_id": object_id,
        "expected_revision": expected,
        "current_revision": current,
        "current_object": copy.deepcopy(current_object),
        "reload_source": prefixes[object_type] + quote(object_id, safe=""),
    }
