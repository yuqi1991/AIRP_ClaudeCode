from __future__ import annotations

import json
import shutil
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engine.runtime import SessionTurnRuntime
from runtime_server import SessionRuntimeServer


SKILLS = __import__("pathlib").Path(__file__).resolve().parents[1]


class _NoopExecutor:
    def run(self, context, tools):
        raise AssertionError("executor is not used by these tests")


def _runtime(tmp_path, styles, *, session_id="local"):
    card = tmp_path / "cards" / "jade-palace"
    (card / "memory").mkdir(parents=True, exist_ok=True)
    return SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        executor=_NoopExecutor(),
        session_id=session_id,
        bootstrap_legacy_history=False,
    )


def _server(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copy(SKILLS / "styles" / "studio.html", styles / "studio.html")
    runtime = _runtime(tmp_path, styles)
    return SessionRuntimeServer(runtime, static_root=str(styles)).start(), styles


def _json_request(method, url, body=None):
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


def _entry(title, *, entry_id=None, order=0, content=None):
    value = {
        "title": title,
        "usage": f"Read for {title}",
        "content": content or f"Facts about {title}",
        "enabled": True,
        "order": order,
        "tags": ["canon"],
    }
    if entry_id:
        value["id"] = entry_id
    return value


def test_worldbook_crud_copy_export_and_global_entry_title_allocation(tmp_path):
    server, _ = _server(tmp_path)
    try:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/worldbooks",
            {"name": "Liyue", "entries": [_entry("Adepti", entry_id="entry-adepti")]},
        )
        assert status == 201
        book_id = created["worldbook"]["id"]
        assert created["worldbook"]["entries"][0]["id"] == "entry-adepti"

        status, second = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/worldbooks",
            {"name": "Shared lore", "entries": [_entry("Adepti"), _entry("Adepti")]},
        )
        assert status == 201
        assert [item["title"] for item in second["worldbook"]["entries"]] == [
            "Adepti-copy",
            "Adepti-copy-2",
        ]
        assert second["renamed_entries"] == [
            {"from": "Adepti", "to": "Adepti-copy"},
            {"from": "Adepti", "to": "Adepti-copy-2"},
        ]

        status, updated = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/worldbooks/{book_id}",
            {"name": "Liyue", "entries": [_entry("Illuminated beasts", entry_id="entry-adepti", content="New canon")]},
        )
        assert status == 200
        assert updated["worldbook"]["entries"][0]["id"] == "entry-adepti"

        status, copied = _json_request(
            "POST", f"{server.base_url}/v1/studio/worldbooks/{book_id}/copy", {}
        )
        assert status == 201
        assert copied["worldbook"]["name"] == "Liyue-copy"
        assert copied["worldbook"]["entries"][0]["title"] == "Illuminated beasts-copy"
        assert copied["worldbook"]["entries"][0]["id"] != "entry-adepti"

        status, exported = _json_request(
            "GET", f"{server.base_url}/v1/studio/worldbooks/{book_id}/export"
        )
        assert status == 200
        assert exported == {
            "format": "airp-worldbook",
            "version": 1,
            "worldbook": {
                "id": book_id,
                "name": "Liyue",
                "entries": [
                    {
                        "id": "entry-adepti",
                        "title": "Illuminated beasts",
                        "usage": "Read for Illuminated beasts",
                        "content": "New canon",
                        "enabled": True,
                        "order": 0,
                        "tags": ["canon"],
                    }
                ],
            },
        }

        status, listed = _json_request("GET", f"{server.base_url}/v1/studio/worldbooks")
        assert status == 200
        assert len(listed["worldbooks"]) == 3
    finally:
        server.stop()


def test_imports_embedded_character_book_world_info_and_airp_json(tmp_path):
    server, _ = _server(tmp_path)
    try:
        imports = [
            {
                "name": "Character lore",
                "project_id": "keqing-project",
                "document": {
                    "spec": "chara_card_v2",
                    "data": {"name": "Keqing", "character_book": {"entries": [
                        {"id": 7, "comment": "Yuheng", "content": "Qixing office", "enabled": True, "insertion_order": 12}
                    ]}},
                },
            },
            {
                "name": "World Info",
                "document": {"entries": {"4": {"uid": 4, "comment": "Rex Lapis", "content": "Geo Archon", "disable": False, "order": 3}}},
            },
            {
                "document": {
                    "format": "airp-worldbook",
                    "version": 1,
                    "worldbook": {"name": "Native", "entries": [_entry("Contracts", entry_id="contract-entry", order=2)]},
                }
            },
        ]
        results = []
        for payload in imports:
            status, result = _json_request(
                "POST", f"{server.base_url}/v1/studio/worldbooks/import", payload
            )
            assert status == 201
            results.append(result)

        assert results[0]["source_format"] == "sillytavern-character-book"
        assert results[0]["worldbook"]["entries"][0] == {
            "id": "7",
            "title": "Yuheng",
            "usage": "",
            "content": "Qixing office",
            "enabled": True,
            "order": 12,
            "tags": [],
        }
        assert results[1]["source_format"] == "sillytavern-world-info"
        assert results[1]["worldbook"]["entries"][0]["title"] == "Rex Lapis"
        assert results[2]["source_format"] == "airp-worldbook"
        assert results[2]["worldbook"]["entries"][0]["id"] == "contract-entry"
        status, project = _json_request(
            "GET", f"{server.base_url}/v1/studio/projects/keqing-project/worldbooks"
        )
        assert status == 200
        assert project["project"]["worldbook_ids"] == [results[0]["worldbook"]["id"]]
    finally:
        server.stop()


def test_project_bindings_protect_delete_and_feed_every_session_next_context(tmp_path):
    server, styles = _server(tmp_path)
    try:
        _, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/worldbooks",
            {"name": "Shared", "entries": [_entry("Harbor", content="Old harbor facts")]},
        )
        book = created["worldbook"]
        status, binding = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/projects/jade-palace/worldbooks",
            {"worldbook_ids": [book["id"]]},
        )
        assert status == 200
        assert binding["project"]["worldbook_ids"] == [book["id"]]
        assert binding["effective_worldbooks"][0]["entries"][0]["content"] == "Old harbor facts"

        status, blocked = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/worldbooks/{book['id']}"
        )
        assert status == 409
        assert blocked["error"] == "worldbook_in_use"
        assert blocked["references"] == [
            {"type": "project", "id": "jade-palace", "name": "jade-palace"}
        ]

        entry = book["entries"][0]
        _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/worldbooks/{book['id']}",
            {"name": "Shared", "entries": [{**entry, "content": "Fresh harbor facts"}]},
        )
        status, refreshed = _json_request(
            "GET", f"{server.base_url}/v1/studio/projects/jade-palace/worldbooks"
        )
        assert status == 200
        assert refreshed["effective_worldbooks"][0]["entries"][0]["content"] == "Fresh harbor facts"
        for session_id in ("session-a", "session-b"):
            runtime = _runtime(tmp_path, styles, session_id=session_id)
            runtime.configure_worldbook_library(
                server.worldbooks.snapshot_for_project, project_id="jade-palace"
            )
            compiled = runtime.compile_opening_context("Begin")
            catalog = next(
                section for section in compiled.manifest["sections"]
                if section["kind"] == "worldbook_catalog"
            )
            assert catalog["content"] == [{"title": "Harbor", "usage": "Read for Harbor"}]
    finally:
        server.stop()


def test_studio_document_wires_worldbook_and_project_binding_contracts(tmp_path):
    server, _ = _server(tmp_path)
    try:
        with urlopen(f"{server.base_url}/studio", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert "Worldbook Library" in page
        assert "/v1/studio/worldbooks" in page
        assert "/v1/studio/projects/" in page
        assert "Import JSON" in page
        assert "Export AIRP JSON" in page
        assert "Project bindings" in page
        assert "importBody.project_id" in page
        assert 'savedNotice("Worldbook copied", payload.renamed_entries)' in page
        assert 'savedNotice("Worldbook imported", payload.renamed_entries)' in page
        assert "Entry title" in page
        assert "Usage" in page
        assert "Enabled" in page
        assert "Order" in page
        assert "Tags" in page
    finally:
        server.stop()
