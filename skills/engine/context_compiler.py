import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    version: str
    token_budget: int
    narrative_policy: str = "你是 AIRP 的叙事导演。"
    max_worldbook_loads: int = 3


@dataclass(frozen=True)
class ContextCompileRequest:
    session_id: str
    task_id: str
    base_revision: int
    player_input: str
    snapshot: dict
    policy: ContextPolicy
    call_ordinal: int = 0
    worldbook_loads: tuple = ()


@dataclass(frozen=True)
class CompiledContext:
    payload: list[dict]
    manifest: dict
    payload_hash: str
    stable_payload_hash: str


def compile_context(request):
    sections = []
    snapshot = request.snapshot
    _add(sections, "narrative_policy", "stable", request.policy.narrative_policy, "role baseline", {"id": "policy", "version": request.policy.version})
    _add(sections, "card_facts", "stable", snapshot.get("card_facts", {}), "card snapshot", snapshot.get("sources", {}).get("card_facts"))
    _add(sections, "settings", "stable", snapshot.get("settings", {}), "session settings", snapshot.get("sources", {}).get("settings"))
    _add(
        sections,
        "worldbook_catalog",
        "stable",
        [
            {"title": entry["title"], "usage": entry.get("usage", "")}
            for entry in snapshot.get("worldbook_catalog", [])
        ],
        "on-demand worldbook selection",
        snapshot.get("sources", {}).get("worldbook_catalog"),
    )
    _add(sections, "card_structure", "stable", snapshot.get("card_structure", {}), "card structure", snapshot.get("sources", {}).get("card_structure"))
    _add(sections, "variable_baseline", "stable", snapshot.get("initvar", {}), "variable schema baseline", snapshot.get("sources", {}).get("initvar"))
    _add(sections, "current_state", "dynamic", snapshot.get("current_state", {}), "active revision state", snapshot.get("sources", {}).get("current_state"))
    _add(sections, "recent_memory", "dynamic", snapshot.get("recent_memory", ""), "recent memory", snapshot.get("sources", {}).get("recent_memory"))
    _add(sections, "recent_turns", "dynamic", snapshot.get("recent_turns", []), "active revision turns", snapshot.get("sources", {}).get("recent_turns"))
    if request.worldbook_loads:
        _add(
            sections,
            "worldbook_entries",
            "dynamic",
            [
                {
                    "title": load["title"],
                    "content": load["content"],
                    "content_hash": load["content_hash"],
                    "catalog_hash": load["catalog_hash"],
                    "reference_hash": load["reference_hash"],
                    "reason": load["reason"],
                }
                for load in request.worldbook_loads
            ],
            "prior exact-title worldbook loads",
            {"id": "worldbook_loads", "version": _hash(request.worldbook_loads)},
        )
    _add(sections, "player_input", "dynamic", request.player_input, "current player command", {"id": "player_input", "version": _hash(request.player_input)})
    budget_decisions = _fit_budget(sections, request.policy.token_budget)
    payload = _payload(sections)
    _fit_payload_budget(sections, request.policy.token_budget, budget_decisions)
    payload = _payload(sections)
    payload_hash = _hash(payload)
    stable_payload_hash = _hash([item for item, section in zip(payload, sections) if section["stability"] == "stable"])
    estimated_tokens = _estimate_tokens(_canonical_json(payload))
    if estimated_tokens > request.policy.token_budget:
        raise ValueError("context token budget exceeded")

    manifest = {
        "schema_version": "context-manifest-v1",
        "session_id": request.session_id,
        "task_id": request.task_id,
        "base_revision": request.base_revision,
        "call_ordinal": request.call_ordinal,
        "policy_version": request.policy.version,
        "token_budget": request.policy.token_budget,
        "estimated_tokens": estimated_tokens,
        "sections": sections,
        "worldbook_loads": list(request.worldbook_loads),
        "payload_hash": payload_hash,
        "stable_payload_hash": stable_payload_hash,
        "budget_decisions": budget_decisions,
    }
    return CompiledContext(payload, manifest, payload_hash, stable_payload_hash)


def replay_payload(manifest):
    return [
        {
            "role": "system" if section["kind"] == "narrative_policy" else "user",
            "content": _canonical_json({"kind": section["kind"], "content": section["content"]}),
        }
        for section in manifest["sections"]
    ]


def _payload(sections):
    return [
        {
            "role": "system" if section["kind"] == "narrative_policy" else "user",
            "content": _canonical_json({"kind": section["kind"], "content": section["content"]}),
        }
        for section in sections
    ]


def _fit_payload_budget(sections, token_budget, decisions):
    payload_tokens = _estimate_tokens(_canonical_json(_payload(sections)))
    if payload_tokens <= token_budget:
        return
    for section in sections:
        if section["kind"] != "recent_memory" or not isinstance(section["content"], str):
            continue
        original = section["content"]
        reduction = (payload_tokens - token_budget) * 4
        keep_bytes = max(0, len(original.encode("utf-8")) - reduction)
        section["content"] = "" if keep_bytes == 0 else original.encode("utf-8")[-keep_bytes:].decode("utf-8", errors="ignore")
        canonical = _canonical_json(section["content"])
        section["content_hash"] = _hash(section["content"])
        section["bytes"] = len(canonical.encode("utf-8"))
        section["estimated_tokens"] = _estimate_tokens(canonical)
        section["disposition"] = "truncated"
        if not any(decision["kind"] == section["kind"] for decision in decisions):
            decisions.append({"kind": section["kind"], "reason": "token budget", "disposition": "truncated"})
        if _estimate_tokens(_canonical_json(_payload(sections))) <= token_budget:
            return
    if _estimate_tokens(_canonical_json(_payload(sections))) > token_budget:
        raise ValueError("context token budget exceeded")


def _fit_budget(sections, token_budget):
    decisions = []
    total = sum(section["estimated_tokens"] for section in sections)
    for section in sections:
        if total <= token_budget:
            break
        if section["kind"] != "recent_memory" or not isinstance(section["content"], str):
            continue
        original = section["content"]
        required_reduction = total - token_budget
        keep_bytes = max(0, len(original.encode("utf-8")) - required_reduction * 4)
        truncated = "" if keep_bytes == 0 else original.encode("utf-8")[-keep_bytes:].decode("utf-8", errors="ignore")
        section["content"] = truncated
        canonical = _canonical_json(truncated)
        section["content_hash"] = _hash(truncated)
        section["bytes"] = len(canonical.encode("utf-8"))
        section["estimated_tokens"] = _estimate_tokens(canonical)
        section["disposition"] = "truncated"
        total = sum(item["estimated_tokens"] for item in sections)
        decisions.append({"kind": section["kind"], "reason": "token budget", "disposition": "truncated"})
    if total > token_budget:
        raise ValueError("context token budget exceeded")
    return decisions


def _add(sections, kind, stability, content, reason, source=None):
    canonical = _canonical_json(content)
    source = source or {"id": kind, "version": _hash(content)}
    sections.append(
        {
            "order": len(sections) + 1,
            "kind": kind,
            "stability": stability,
            "content": content,
            "source": {**source, "hash": _hash(content)},
            "content_hash": _hash(content),
            "inclusion_reason": reason,
            "bytes": len(canonical.encode("utf-8")),
            "estimated_tokens": _estimate_tokens(canonical),
            "disposition": "included",
        }
    )


def _estimate_tokens(content):
    return (len(content.encode("utf-8")) + 3) // 4


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
