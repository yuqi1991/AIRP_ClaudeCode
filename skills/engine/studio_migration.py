"""Bootstrap the Studio library from the existing runtime config files.

The first Studio implementation deliberately introduced a separate library
root. Existing cards still start with ``styles/graphs`` and ``settings.json``;
this module performs a one-way, idempotent import so those cards are visible in
Studio without overwriting definitions the user has already edited there.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from engine.provider import runtime_provider_api_key


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_GENERATION_FIELDS = {
    "temperature",
    "max_output_tokens",
    "top_p",
    "stop",
    "reasoning_effort",
    "seed",
}


def bootstrap_legacy_runtime_library(
    *,
    static_root: str | Path,
    provider_store,
    secret_store,
    agent_store,
    graph_store,
    project_store=None,
    project_id: str | None = None,
    card_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import legacy graphs/providers/agents once and return a small summary."""

    root = Path(static_root).resolve()
    settings = _read_object(root / "settings.json")
    runtime_settings = settings.get("runtime") if isinstance(settings.get("runtime"), dict) else {}
    selected_graph_id = _safe_id(runtime_settings.get("graph_id") or "default")
    selected_preset_id = _safe_id(runtime_settings.get("preset_id") or "default")
    legacy_graphs = _legacy_graphs(root / "graphs")
    if not legacy_graphs:
        return {"graphs": 0, "agents": 0, "providers": 0, "project": False}

    provider_specs = _collect_provider_specs(legacy_graphs, settings)
    provider_ids: dict[str, str] = {}
    providers_created = 0
    for provider_key, spec in provider_specs.items():
        profile_id = _provider_id(provider_key)
        provider_ids[provider_key] = profile_id
        try:
            provider_store.get_profile(profile_id)
        except Exception:
            try:
                provider_store.create_profile(
                    {
                        "id": profile_id,
                        "name": f"Legacy {provider_key}",
                        "base_url": spec["base_url"],
                        "api_format": spec["api_format"],
                        "enabled": True,
                        "model_ids": spec["model_ids"],
                    }
                )
                providers_created += 1
            except Exception:
                continue
        key = _provider_key_from_environment(provider_key)
        if key and not secret_store.has(profile_id):
            try:
                secret_store.set(profile_id, key)
            except Exception:
                pass

    agent_ids: dict[tuple[str, str], str] = {}
    agents_created = 0
    for graph_id, graph in legacy_graphs.items():
        enabled_nodes = [node for node in graph["nodes"] if node.get("enabled", True) is not False]
        final_node_id = (enabled_nodes[-1].get("id") or enabled_nodes[-1].get("node_id")) if enabled_nodes else None
        for node in graph["nodes"]:
            node_id = _safe_id(node.get("id") or node.get("node_id") or f"node-{node.get('order', 0)}")
            explicit_agent_id = node.get("agent_id") or node.get("agent_definition_id")
            agent_id = _safe_id(explicit_agent_id) if isinstance(explicit_agent_id, str) else _safe_id(f"legacy-{graph_id}-{node_id}")
            agent_ids[(graph_id, node_id)] = agent_id
            is_director = node.get("role") == "narrative_director" or node_id == _safe_id(final_node_id)
            legacy_instruction = _legacy_agent_instruction(settings) if is_director else ""
            node_instruction = node.get("instruction") if isinstance(node.get("instruction"), str) else ""
            effective_instruction = node_instruction.strip() or legacy_instruction
            try:
                existing = agent_store.get_agent(agent_id)
                if effective_instruction and not str(existing.get("instruction") or "").strip():
                    agent_store.update_agent(agent_id, {"instruction": effective_instruction})
                continue
            except Exception:
                pass
            provider_key = _provider_key(node, settings)
            generation = node.get("generation") if isinstance(node.get("generation"), dict) else {}
            generation = {key: value for key, value in generation.items() if key in _GENERATION_FIELDS}
            for key in _GENERATION_FIELDS:
                if key in node and key not in generation:
                    generation[key] = node[key]
            advanced = node.get("advanced")
            if not isinstance(advanced, dict):
                advanced = node.get("advanced_parameters") if isinstance(node.get("advanced_parameters"), dict) else {}
            payload = {
                "agent_id": agent_id,
                "name": str(node.get("name") or node.get("label") or node.get("role") or node_id),
                "instruction": effective_instruction,
                "prompt_preset_id": selected_preset_id,
                "provider_profile_id": provider_ids.get(provider_key),
                "model_id": node.get("model") if isinstance(node.get("model"), str) else None,
                "generation": generation,
                "advanced": advanced,
                "tool_allowlist": node.get("tool_allowlist") if isinstance(node.get("tool_allowlist"), list) else [],
            }
            try:
                agent_store.create_agent(payload)
                agents_created += 1
            except Exception:
                # A malformed legacy node should not hide other valid configs.
                continue

    graphs_created = 0
    for graph_id, graph in legacy_graphs.items():
        try:
            graph_store.get_graph(graph_id)
            continue
        except Exception:
            pass
        nodes = []
        for index, node in enumerate(graph["nodes"]):
            node_id = _safe_id(node.get("id") or node.get("node_id") or f"node-{index}")
            nodes.append(
                {
                    "node_id": node_id,
                    "agent_id": agent_ids.get((graph_id, node_id), _safe_id(f"legacy-{graph_id}-{node_id}")),
                    "label": node.get("label") if isinstance(node.get("label"), str) else None,
                    "enabled": node.get("enabled", True) is not False,
                    "order": index,
                    "provider_profile_id": provider_ids.get(_provider_key(node, settings)),
                    "model_id": node.get("model") if isinstance(node.get("model"), str) else None,
                }
            )
        enabled = [node for node in nodes if node["enabled"]]
        if not enabled:
            continue
        payload = {
            "id": graph_id,
            "name": str(graph.get("name") or f"Legacy {graph_id}"),
            "version": str(graph.get("version") or "1"),
            "mode": "sequential",
            "nodes": nodes,
            "output_node_id": enabled[-1]["node_id"],
        }
        try:
            graph_store.create_graph(payload)
            graphs_created += 1
        except Exception:
            continue

    project_created = False
    if project_store is not None and isinstance(project_id, str) and project_id:
        try:
            project = project_store.get_project(project_id)
            if not project.get("graph_id"):
                project_store.update_project(
                    project_id,
                    {
                        "graph_id": selected_graph_id
                        if selected_graph_id in legacy_graphs
                        else next(iter(legacy_graphs), None)
                    },
                )
        except Exception:
            facts = card_facts if isinstance(card_facts, dict) else {}
            project_payload = {
                "id": project_id,
                "name": str(facts.get("name") or project_id),
                "card": facts,
                "graph_id": selected_graph_id if selected_graph_id in legacy_graphs else next(iter(legacy_graphs), None),
            }
            try:
                project_store.create_project(project_payload)
                project_created = True
            except Exception:
                pass

    return {
        "graphs": graphs_created,
        "agents": agents_created,
        "providers": providers_created,
        "project": project_created,
    }


def _legacy_agent_instruction(settings: dict[str, Any]) -> str:
    """Turn the removed game-side writing controls into editable Agent text."""
    preferences: list[str] = []
    labels = (
        ("style", "文风偏好"),
        ("nsfw", "NSFW尺度"),
        ("person", "叙事人称"),
        ("wordCount", "目标回复长度"),
    )
    for key, label in labels:
        value = settings.get(key)
        if value not in (None, ""):
            preferences.append(f"- {label}: {value}")
    if settings.get("antiImpersonation") is True:
        preferences.append("- 不代替玩家发言、行动或决定")
    if settings.get("bgNpc") is True:
        preferences.append("- 在场景需要时保持背景 NPC 活跃")
    if not preferences:
        return ""
    return (
        "遵循以下写作偏好。这些都是软约束：优先服从项目卡、世界书、当前上下文和用户当轮指令；"
        "在不冲突时尽量满足。\n" + "\n".join(preferences)
    )


def _legacy_graphs(root: Path) -> dict[str, dict[str, Any]]:
    graphs: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return graphs
    for path in sorted(root.glob("*.json")):
        raw = _read_object(path)
        nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
        if not nodes:
            continue
        graph_id = _safe_id(raw.get("id") or path.stem)
        graphs[graph_id] = {**raw, "id": graph_id, "nodes": nodes}
    return graphs


def _collect_provider_specs(graphs: dict[str, dict[str, Any]], settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for graph in graphs.values():
        for node in graph["nodes"]:
            provider = _provider_key(node, settings)
            spec = specs.setdefault(
                provider,
                {
                    "base_url": _provider_base_url(provider, node, settings),
                    "api_format": str(node.get("api_format") or (settings.get("provider") or {}).get("api_format") or "chat_completions"),
                    "model_ids": [],
                },
            )
            model = node.get("model")
            if isinstance(model, str) and model.strip() and model.strip() not in spec["model_ids"]:
                spec["model_ids"].append(model.strip())
    return specs


def _provider_key(node: dict[str, Any], settings: dict[str, Any]) -> str:
    value = node.get("provider") or node.get("provider_id")
    if isinstance(value, str) and value.strip():
        return _safe_id(value.strip())
    return _safe_id((settings.get("provider") or {}).get("id") or "deepseek")


def _provider_base_url(provider: str, node: dict[str, Any], settings: dict[str, Any]) -> str:
    value = node.get("base_url")
    provider_settings = settings.get("provider") if isinstance(settings.get("provider"), dict) else {}
    value = value or provider_settings.get("base_url")
    value = value or ("https://api.deepseek.com" if provider == "deepseek" else "https://api.example.com/v1")
    return str(value).strip().rstrip("/")


def _provider_key_from_environment(provider: str) -> str | None:
    override = runtime_provider_api_key(provider)
    if override:
        return override
    suffix = re.sub(r"[^A-Za-z0-9]", "_", provider).upper()
    return os.environ.get(f"{suffix}_API_KEY") or (os.environ.get("DEEPSEEK_API_KEY") if provider == "deepseek" else None)


def _provider_id(provider: str) -> str:
    return _safe_id(f"legacy-{provider}")


def _safe_id(value: Any) -> str:
    text = str(value or "legacy").strip()
    text = _SAFE_ID.sub("-", text).strip("-.") or "legacy"
    return text[:64]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
