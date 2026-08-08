from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.host.rp.session_runtime import SessionTurnRuntime  # noqa: E402
from airp.server import SessionRuntimeServer  # noqa: E402


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


def test_server_exposes_the_complete_keyless_suite_through_ordinary_studio_crud(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"HTTP Suite"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        endpoints = {
            "providers": ("profiles", "default-deepseek", "profile"),
            "agents": ("agents", "default-writer", "agent"),
            "regex-collections": ("collections", "default-content", "collection"),
            "graphs": ("graphs", "default-two-round-review", "graph"),
        }
        fetched = {}
        for endpoint, (list_key, object_id, get_key) in endpoints.items():
            base_url = f"{server.base_url}/v1/studio/{endpoint}"
            status, listed = _json_request("GET", base_url)
            assert status == 200
            assert object_id in {
                item.get("id", item.get("agent_id")) for item in listed[list_key]
            }
            status, payload = _json_request("GET", f"{base_url}/{object_id}")
            assert status == 200
            fetched[get_key] = payload[get_key]

        assert fetched["profile"]["key_configured"] is False
        assert fetched["agent"]["provider_profile_id"] == fetched["profile"]["id"]
        assert fetched["agent"]["regex_collection_id"] == fetched["collection"]["id"]
        assert fetched["graph"]["output_node_id"] == "final-writer"

        writer_url = f"{server.base_url}/v1/studio/agents/default-writer"
        status, updated = _json_request(
            "PUT",
            writer_url,
            {
                "expected_revision": fetched["agent"]["revision"],
                "instruction": "用户通过普通 Studio API 编辑后的 Instruction",
            },
        )
        assert status == 200
        assert updated["agent"]["instruction"] == "用户通过普通 Studio API 编辑后的 Instruction"
        status, reloaded = _json_request("GET", writer_url)
        assert status == 200
        assert reloaded["agent"]["instruction"] == updated["agent"]["instruction"]
