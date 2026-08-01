import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from airp.engine.macros import build_context, expand_template


# Compile-time only. No date/time/cwd/random — those are non-deterministic
# (pi-rp coding-agent macros explicitly excluded; see ADR-0009).
MACROS_VERSION = "macros-v1"


@dataclass(frozen=True)
class ContextPolicy:
    version: str
    token_budget: int
    # The engine owns context assembly only. Writing instructions belong to
    # the selected Agent/Project and must never be synthesized here.
    narrative_policy: str = ""
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
    layout: Any = None  # Optional[ContextLayout]; default → DEFAULT_CONTEXT_LAYOUT


@dataclass(frozen=True)
class CompiledContext:
    payload: list[dict]
    manifest: dict
    payload_hash: str
    stable_payload_hash: str


# (request) -> (content, source_meta | None)
SlotResolver = Callable[[ContextCompileRequest], tuple[Any, Optional[dict]]]
# (request) -> bool
IncludeWhen = Callable[[ContextCompileRequest], bool]


@dataclass(frozen=True)
class ManifestSlot:
    """One declarative Manifest section.

    resolve(request) returns (content, source_meta). source_meta may be None;
    _add then synthesizes id/version from content. When expand_macros is True,
    string content is expanded at compile time BEFORE hashing.
    """

    kind: str
    stability: str
    inclusion_reason: str
    resolve: SlotResolver
    enabled: bool = True
    include_when: Optional[IncludeWhen] = None
    expand_macros: bool = False
    role: str | None = None
    prompt_entry: dict | None = None


@dataclass(frozen=True)
class ContextLayout:
    """Internal ordered layout for generic context data, never writing policy."""

    id: str
    version: str
    slots: tuple


def _always(_request):
    return True


def _when_worldbook_loads(request):
    return bool(request.worldbook_loads)


def _resolve_narrative_policy(request):
    return request.policy.narrative_policy, {"id": "policy", "version": request.policy.version}


def _resolve_card_facts(request):
    return request.snapshot.get("card_facts", {}), request.snapshot.get("sources", {}).get("card_facts")


def _resolve_settings(request):
    return request.snapshot.get("settings", {}), request.snapshot.get("sources", {}).get("settings")


def _resolve_worldbook_catalog(request):
    content = [
        {"title": entry["title"], "usage": entry.get("usage", "")}
        for entry in request.snapshot.get("worldbook_catalog", [])
    ]
    return content, request.snapshot.get("sources", {}).get("worldbook_catalog")


def _resolve_card_structure(request):
    return request.snapshot.get("card_structure", {}), request.snapshot.get("sources", {}).get("card_structure")


def _resolve_variable_baseline(request):
    return request.snapshot.get("initvar", {}), request.snapshot.get("sources", {}).get("initvar")


def _resolve_current_state(request):
    return request.snapshot.get("current_state", {}), request.snapshot.get("sources", {}).get("current_state")


def _resolve_recent_memory(request):
    return request.snapshot.get("recent_memory", ""), request.snapshot.get("sources", {}).get("recent_memory")


def _resolve_recent_turns(request):
    return request.snapshot.get("recent_turns", []), request.snapshot.get("sources", {}).get("recent_turns")


def _resolve_worldbook_entries(request):
    content = [
        {
            "title": load["title"],
            "content": load["content"],
            "content_hash": load["content_hash"],
            "catalog_hash": load["catalog_hash"],
            "reference_hash": load["reference_hash"],
            "reason": load["reason"],
        }
        for load in request.worldbook_loads
    ]
    return content, {"id": "worldbook_loads", "version": _hash(request.worldbook_loads)}


def _resolve_player_input(request):
    return request.player_input, {"id": "player_input", "version": _hash(request.player_input)}


def static_slot(kind, stability, inclusion_reason, content, source_id=None, source_version="static", enabled=True, expand_macros=False):
    """Build a fixed generic context slot for layout-level tests and extensions."""

    def resolve(_request):
        return content, {"id": source_id or kind, "version": source_version}

    return ManifestSlot(
        kind=kind,
        stability=stability,
        inclusion_reason=inclusion_reason,
        resolve=resolve,
        enabled=enabled,
        expand_macros=expand_macros,
    )


DEFAULT_CONTEXT_LAYOUT = ContextLayout(
    id="default-context-layout",
    version="context-manifest-v1",
    slots=(
        ManifestSlot("narrative_policy", "stable", "role baseline", _resolve_narrative_policy),
        ManifestSlot("card_facts", "stable", "card snapshot", _resolve_card_facts),
        ManifestSlot("settings", "stable", "session settings", _resolve_settings),
        ManifestSlot("worldbook_catalog", "stable", "on-demand worldbook selection", _resolve_worldbook_catalog),
        ManifestSlot("card_structure", "stable", "card structure", _resolve_card_structure),
        ManifestSlot("variable_baseline", "stable", "variable schema baseline", _resolve_variable_baseline),
        ManifestSlot("current_state", "dynamic", "active revision state", _resolve_current_state),
        ManifestSlot("recent_memory", "dynamic", "recent memory", _resolve_recent_memory),
        ManifestSlot("recent_turns", "dynamic", "active revision turns", _resolve_recent_turns),
        ManifestSlot(
            "worldbook_entries",
            "dynamic",
            "prior exact-title worldbook loads",
            _resolve_worldbook_entries,
            include_when=_when_worldbook_loads,
        ),
        ManifestSlot("player_input", "dynamic", "current player command", _resolve_player_input),
    ),
)


def resolve_layout(request):
    return request.layout if request.layout is not None else DEFAULT_CONTEXT_LAYOUT


def compose_layout(base, *, id, version, disable_kinds=(), slot_overrides=(), append_slots=()):
    """Compose an internal generic context layout without touching compilation.

    - disable_kinds: kinds to flip enabled=False
    - slot_overrides: ManifestSlot instances that replace same-kind slots in place
    - append_slots: extra slots appended after the base list (before nothing — order as given)
    """
    override_by_kind = {slot.kind: slot for slot in slot_overrides}
    disable = set(disable_kinds)
    slots = []
    for slot in base.slots:
        if slot.kind in override_by_kind:
            slots.append(override_by_kind[slot.kind])
        elif slot.kind in disable:
            slots.append(replace(slot, enabled=False))
        else:
            slots.append(slot)
    for slot in append_slots:
        slots.append(slot)
    return ContextLayout(id=id, version=version, slots=tuple(slots))


def macro_context(request):
    """Build macro substitution map from snapshot/request only (no ambient state)."""
    snapshot = request.snapshot or {}
    settings = snapshot.get("settings") or {}
    card_facts = snapshot.get("card_facts") or {}

    def _as_str(value):
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return _canonical_json(value)
        return str(value)

    macro_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key not in {"worldbook_reference", "worldbook_user"}
    }
    context = build_context(macro_snapshot, request={
        "player_input": request.player_input,
        "user": settings.get("user") or snapshot.get("user") or "",
    })
    # Keep generic card/player aliases for migrated instructions. Legacy
    # style/nsfw/person controls are deliberately not synthesized by the
    # compiler; users can express those requirements in Agent instructions.
    context.update({
        "charName": _as_str(settings.get("charName") or card_facts.get("name") or ""),
        "user": _as_str(settings.get("user") or snapshot.get("user") or ""),
    })
    return context


def expand_macros(value, request):
    """Expand {{macro}} placeholders in strings at compile time. Non-strings pass through."""
    return expand_template(value, macro_context(request), preserve_unknown=True)


def compile_context(request):
    layout = resolve_layout(request)
    sections = []
    for slot in layout.slots:
        if not slot.enabled:
            continue
        include_when = slot.include_when or _always
        if not include_when(request):
            continue
        content, source = slot.resolve(request)
        if slot.expand_macros:
            content = expand_macros(content, request)
            if source is not None:
                source = {**source, "version": f"{source.get('version', '')}|{MACROS_VERSION}"}
            else:
                source = {"id": slot.kind, "version": MACROS_VERSION}
        _add(
            sections,
            slot.kind,
            slot.stability,
            content,
            slot.inclusion_reason,
            source,
            role=slot.role,
            prompt_entry=slot.prompt_entry,
        )
    return _finalize_context(request, layout, sections)


def _finalize_context(request, layout, sections):
    budget_decisions = _fit_budget(sections, request.policy.token_budget)
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
        "context_layout": {"id": layout.id, "version": layout.version},
        "macros_version": MACROS_VERSION,
    }
    return CompiledContext(payload, manifest, payload_hash, stable_payload_hash)


def _payload(sections):
    return [
        {
            "role": section.get("role") or ("system" if section["kind"] == "narrative_policy" else "user"),
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


def _add(sections, kind, stability, content, reason, source=None, *, role=None, prompt_entry=None):
    canonical = _canonical_json(content)
    content_hash = _hash(content)
    source = source or {"id": kind, "version": content_hash}
    section = {
        "order": len(sections) + 1,
        "kind": kind,
        "stability": stability,
        "content": content,
        "source": {**source, "hash": content_hash},
        "content_hash": content_hash,
        "inclusion_reason": reason,
        "bytes": len(canonical.encode("utf-8")),
        "estimated_tokens": _estimate_tokens(canonical),
        "disposition": "included",
    }
    if role is not None:
        section["role"] = role
    if prompt_entry is not None:
        section["prompt_entry"] = prompt_entry
    sections.append(section)


def _estimate_tokens(content):
    return (len(content.encode("utf-8")) + 3) // 4


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
