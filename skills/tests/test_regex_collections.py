from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills"))

from airp.application import Application  # noqa: E402
from airp.engine.regex_collections import (  # noqa: E402
    RegexCollectionError,
    RegexCollectionLibrary,
)
from airp.workspace import Workspace  # noqa: E402
from engine.runtime import SessionTurnRuntime  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


def _write_card(card: Path) -> None:
    card.mkdir(parents=True, exist_ok=True)
    (card / "memory").mkdir(exist_ok=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Test"}), encoding="utf-8")


def _server(tmp_path: Path) -> SessionRuntimeServer:
    styles = tmp_path / "styles"
    styles.mkdir(exist_ok=True)
    shutil.copy(ROOT / "src" / "airp" / "web" / "index.html", styles / "index.html")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )
    return SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace")


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


def _collection_payload() -> dict:
    return {
        "id": "extract-final-prose",
        "name": "Extract final prose",
        "rules": [
            {
                "id": "unwrap-content",
                "name": "Unwrap content tag",
                "enabled": True,
                "target": "output",
                "pattern": "^[\\s\\S]*?<content>([\\s\\S]*?)</content>[\\s\\S]*$",
                "flags": "",
                "replacement": "$1",
            },
            {
                "name": "disabled rule",
                "enabled": False,
                "target": "both",
                "pattern": "ignored",
                "flags": "gi",
                "replacement": "",
            },
        ],
    }


def test_regex_collection_library_persists_ordered_rules_and_normalizes_ids(tmp_path: Path):
    library = RegexCollectionLibrary(tmp_path / "static", workspace=Workspace.from_root(tmp_path / "workspace"))

    created = library.create_collection(_collection_payload())

    assert created["id"] == "extract-final-prose"
    assert created["name"] == "Extract final prose"
    assert [rule["name"] for rule in created["rules"]] == ["Unwrap content tag", "disabled rule"]
    assert created["rules"][1]["id"].startswith("rule-")
    assert created["rules"][1]["enabled"] is False
    assert created["rules"][1]["target"] == "both"
    assert library.get_collection("extract-final-prose") == created

    restarted = RegexCollectionLibrary(tmp_path / "static", workspace=Workspace.from_root(tmp_path / "workspace"))
    assert restarted.list_collections() == [created]


def test_regex_collection_copy_suffix_and_agent_reference_deletion_protection(tmp_path: Path):
    references = {"extract-final-prose": [{"type": "agent", "id": "writer", "name": "Writer"}]}
    library = RegexCollectionLibrary(
        tmp_path / "static",
        workspace=Workspace.from_root(tmp_path / "workspace"),
        reference_callback=lambda collection_id: references.get(collection_id, []),
    )
    original = library.create_collection(_collection_payload())

    copied = library.copy_collection(original["id"])
    copied_again = library.copy_collection(original["id"])

    assert copied["name"] == "Extract final prose-copy"
    assert copied_again["name"] == "Extract final prose-copy-2"
    assert copied["id"] != original["id"]
    assert copied["rules"][0]["id"] != original["rules"][0]["id"]

    with pytest.raises(RegexCollectionError) as exc_info:
        library.delete_collection(original["id"])
    assert exc_info.value.code == "regex_collection_in_use"
    assert exc_info.value.status == 409
    assert exc_info.value.references == references[original["id"]]

    library.delete_collection(copied["id"])
    assert [item["id"] for item in library.list_collections()] == [original["id"], copied_again["id"]]


def test_application_assembles_regex_collection_library_in_workspace(tmp_path: Path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    workspace = Workspace.from_root(tmp_path / "workspace")

    application = Application.assemble(static_root=static_root, workspace=workspace)

    assert application.regex_collections.library_root == workspace.regex_collections_root


def test_regex_collection_http_crud_and_javascript_test_seam(tmp_path: Path):
    with _server(tmp_path) as server:
        endpoint = f"{server.base_url}/v1/studio/regex-collections"
        status, created = _json_request("POST", endpoint, _collection_payload())
        assert status == 201
        collection = created["collection"]
        assert collection["id"] == "extract-final-prose"

        status, listed = _json_request("GET", endpoint)
        assert status == 200
        assert [item["id"] for item in listed["collections"]] == ["extract-final-prose"]

        status, fetched = _json_request("GET", f"{endpoint}/extract-final-prose")
        assert status == 200
        assert fetched["collection"] == collection

        status, updated = _json_request(
            "PUT",
            f"{endpoint}/extract-final-prose",
            {"name": "Final prose", "rules": collection["rules"][:1]},
        )
        assert status == 200
        assert updated["collection"]["name"] == "Final prose"
        assert len(updated["collection"]["rules"]) == 1

        status, copied = _json_request("POST", f"{endpoint}/extract-final-prose/copy", {})
        assert status == 201
        assert copied["collection"]["name"] == "Final prose-copy"

        status, tested = _json_request(
            "POST",
            f"{endpoint}/extract-final-prose/test",
            {"target": "output", "text": "<content>hello</content>"},
        )
        assert status == 200
        assert tested["result"]["text"] == "hello"
        assert tested["result"]["rules"][0]["changed"] is True

        status, deleted = _json_request("DELETE", f"{endpoint}/extract-final-prose")
        assert status == 200
        assert deleted["deleted_id"] == "extract-final-prose"

        status, missing = _json_request("GET", f"{endpoint}/extract-final-prose")
        assert status == 404
        assert missing["error"] == "regex_collection_not_found"


def test_regex_collection_delete_reports_agent_binding_reference(tmp_path: Path):
    with _server(tmp_path) as server:
        endpoint = f"{server.base_url}/v1/studio/regex-collections"
        status, created = _json_request("POST", endpoint, _collection_payload())
        assert status == 201
        collection_id = created["collection"]["id"]

        status, agent = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {
                "agent_id": "writer",
                "name": "Writer",
                "instruction": "Write",
                "regex_collection_id": collection_id,
            },
        )
        assert status == 201
        assert agent["agent"]["regex_collection_id"] == collection_id

        status, blocked = _json_request("DELETE", f"{endpoint}/{collection_id}")
        assert status == 409
        assert blocked["error"] == "regex_collection_in_use"
        assert blocked["references"] == [{"type": "agent", "id": "writer", "name": "Writer"}]

        status, _ = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/agents/writer",
            {"regex_collection_id": None},
        )
        assert status == 200

        status, deleted = _json_request("DELETE", f"{endpoint}/{collection_id}")
        assert status == 200
        assert deleted["deleted_id"] == collection_id


def test_regex_collection_library_rejects_invalid_schema(tmp_path: Path):
    library = RegexCollectionLibrary(tmp_path / "static")

    with pytest.raises(RegexCollectionError, match="name must be a non-empty string"):
        library.create_collection({"name": "", "rules": []})
    with pytest.raises(RegexCollectionError, match="rules must be an array"):
        library.create_collection({"name": "Bad", "rules": {}})
    with pytest.raises(RegexCollectionError, match="target must be input, output, or both"):
        library.create_collection(
            {
                "name": "Bad",
                "rules": [{"name": "bad", "target": "sideways", "pattern": "x", "replacement": ""}],
            }
        )
