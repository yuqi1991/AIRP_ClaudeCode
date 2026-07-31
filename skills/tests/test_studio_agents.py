from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine.runtime import SessionTurnRuntime
from runtime_server import SessionRuntimeServer


SKILLS = Path(__file__).resolve().parents[1]


def _write_card(card: Path) -> None:
    card.mkdir(parents=True, exist_ok=True)
    (card / "memory").mkdir(exist_ok=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Test"}), encoding="utf-8")


def _server(tmp_path: Path) -> SessionRuntimeServer:
    styles = tmp_path / "styles"
    styles.mkdir(exist_ok=True)
    shutil.copy(SKILLS / "styles" / "studio.html", styles / "studio.html")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )
    return SessionRuntimeServer(runtime, static_root=styles)


def _json_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _agent_payload() -> dict:
    return {
        "agent_id": "writer-agent",
        "name": "Writer",
        "instruction": "Write the scene.",
        "provider_profile_id": "shared-provider",
        "model_id": "writer-model",
        "generation": {
            "temperature": 0.2,
            "max_output_tokens": 500,
            "top_p": 0.9,
            "stop": ["<END>"],
            "reasoning_effort": "low",
            "seed": 7,
        },
        "advanced": {
            "temperature": 0.8,
            "response_format": {"type": "json_object"},
            "model": "attacker-model",
            "stream": False,
            "input": "attacker-input",
            "tools": ["attacker-tool"],
            "base_url": "https://attacker.example",
            "trace_id": "attacker-trace",
        },
        "tool_allowlist": ["get_recent_memory"],
    }


def test_agent_crud_stable_id_copy_suffix_and_role_removed(tmp_path: Path):
    with _server(tmp_path) as server:
        status, created = _json_request("POST", f"{server.base_url}/v1/studio/agents", _agent_payload())
        assert status == 201
        agent = created["agent"]
        assert agent["agent_id"] == "writer-agent"
        assert agent["id"] == "writer-agent"
        assert agent["name"] == "Writer"
        assert agent["generation"]["max_output_tokens"] == 500
        assert agent["advanced"]["response_format"] == {"type": "json_object"}
        assert "role" not in agent

        status, updated = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/agents/writer-agent",
            {"name": "Editor", "instruction": "Edit the scene."},
        )
        assert status == 200
        assert updated["agent"]["agent_id"] == "writer-agent"
        assert updated["agent"]["name"] == "Editor"
        assert updated["agent"]["instruction"] == "Edit the scene."

        status, copied = _json_request(
            "POST", f"{server.base_url}/v1/studio/agents/writer-agent/copy", {}
        )
        assert status == 201
        assert copied["agent"]["agent_id"] != "writer-agent"
        assert copied["agent"]["name"] == "Editor-copy"

        status, copied_again = _json_request(
            "POST", f"{server.base_url}/v1/studio/agents/writer-agent/copy", {}
        )
        assert status == 201
        assert copied_again["agent"]["name"] == "Editor-copy-2"

        status, invalid = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {"name": "Legacy", "role": "writer"},
        )
        assert status == 400
        assert invalid["error"] == "agent_role_removed"

    with _server(tmp_path) as restarted:
        status, fetched = _json_request(
            "GET", f"{restarted.base_url}/v1/studio/agents/writer-agent"
        )
        assert status == 200
        assert fetched["agent"]["agent_id"] == "writer-agent"
        assert fetched["agent"]["name"] == "Editor"


def test_agent_prompt_preview_exposes_provenance_effective_config_and_tools(tmp_path: Path):
    with _server(tmp_path) as server:
        _, created = _json_request("POST", f"{server.base_url}/v1/studio/agents", _agent_payload())
        status, preview_payload = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents/writer-agent/prompt-preview",
            {
                "project_input": "Player enters the room.",
                "handoff": {"source_node_id": "planner", "artifact": "A plan."},
                "output_contract": {"kind": "narrative_draft", "content_type": "text/html"},
            },
        )
        assert status == 200
        preview = preview_payload["preview"]
        assert [item["kind"] for item in preview["provenance"]] == [
            "instruction",
            "project_input",
            "handoff",
            "tool_protocol",
            "output_contract",
        ]
        assert [item["source"] for item in preview["messages"]] == [
            "instruction",
            "project_input",
            "handoff",
            "tool_protocol",
            "output_contract",
        ]
        assert preview["effective_config"]["generation"]["temperature"] == 0.8
        assert preview["effective_config"]["generation"]["max_output_tokens"] == 500
        assert preview["effective_config"]["model_id"] == "writer-model"
        assert preview["effective_config"]["stream"] is True
        assert [tool["name"] for tool in preview["effective_config"]["tools"]] == [
            "get_recent_memory"
        ]
        assert "model" in preview["ignored_advanced_fields"]
        assert "stream" in preview["ignored_advanced_fields"]
        assert preview["effective_config"]["response_format"] == {"type": "json_object"}


def test_agent_delete_is_blocked_by_graph_reference(tmp_path: Path):
    with _server(tmp_path) as server:
        _, created = _json_request("POST", f"{server.base_url}/v1/studio/agents", _agent_payload())
        graph_root = tmp_path / "styles" / "graphs"
        graph_root.mkdir()
        (graph_root / "writing.json").write_text(
            json.dumps(
                {
                    "id": "writing",
                    "name": "Writing Graph",
                    "nodes": [{"id": "writer-node", "agent_id": created["agent"]["agent_id"]}],
                }
            ),
            encoding="utf-8",
        )
        status, blocked = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/agents/{created['agent']['agent_id']}"
        )
        assert status == 409
        assert blocked["error"] == "agent_in_use"
        assert blocked["references"] == [
            {"type": "graph", "id": "writing", "name": "Writing Graph"}
        ]

        (graph_root / "writing.json").unlink()
        status, deleted = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/agents/{created['agent']['agent_id']}"
        )
        assert status == 200
        assert deleted["deleted_id"] == created["agent"]["agent_id"]


def test_agents_view_and_api_contract_are_wired(tmp_path: Path):
    with _server(tmp_path) as server:
        with urlopen(f"{server.base_url}/studio", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert response.status == 200
    assert "Agents" in page
    assert 'id="agent-form"' in page
    assert 'const endpoint = "/v1/studio/agents"' in page
    assert "prompt-preview" in page
    assert "Advanced JSON" in page
    assert "Tool allowlist" in page
