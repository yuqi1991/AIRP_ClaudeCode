from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer

from test_agent_studio_golden_path import _HeadlessBrowser, _connect, _json_request


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "airp" / "web"


@pytest.mark.skipif(
    shutil.which("google-chrome") is None or _connect is None,
    reason="requires google-chrome and websockets",
)
def test_game_worldbook_drawer_reads_edits_and_binds_existing_api(tmp_path: Path):
    styles = tmp_path / "styles"
    shutil.copytree(WEB_ROOT, styles)
    card = tmp_path / "drawer-card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Drawer Card"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/worldbooks",
            {"name": "Harbor Lore", "entries": [{"title": "Harbor", "usage": "Read near docks", "content": "Foggy docks.", "enabled": True, "order": 0, "tags": ["canon"]}]},
        )
        assert status == 201
        book_id = created["worldbook"]["id"]
        status, _project = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "drawer-project", "name": "Drawer Project", "worldbook_ids": []},
        )
        assert status == 201

        with _HeadlessBrowser(f"{server.base_url}/", tmp_path / "chrome-profile") as browser:
            browser.wait_for('document.querySelector("#studio-worldbooks-toggle")')
            browser.click("#studio-worldbooks-toggle")
            browser.wait_for('document.querySelector("[data-airp-drawer-view=\\"worldbooks\\"]")')
            browser.wait_for('document.querySelector("#worldbook-drawer-list [data-worldbook-id]")')
            assert browser.evaluate("document.getElementById('worldbook-drawer-list').textContent.includes('Harbor Lore')")
            assert browser.evaluate("document.getElementById('worldbook-drawer-entry-sort').options.length === 3")

            browser.click(f'#worldbook-drawer-list [data-worldbook-id="{book_id}"]')
            browser.wait_for("document.getElementById('worldbook-drawer-name').value === 'Harbor Lore'")
            assert browser.evaluate("[...document.querySelectorAll('#worldbook-drawer-entries textarea')].some((input) => input.value === 'Foggy docks.')")

            browser.evaluate(
                """(() => {
                    const project = document.getElementById('worldbook-drawer-project');
                    project.value = 'drawer-project';
                    project.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                })()"""
            )
            browser.wait_for("document.querySelector('#worldbook-drawer-project option:checked').value === 'drawer-project'")
            browser.wait_for("document.querySelectorAll('#worldbook-drawer-bindings input').length === 1")
            browser.click("#worldbook-drawer-bindings input")
            browser.click("#worldbook-drawer-save-bindings")
            browser.wait_for("document.getElementById('worldbook-drawer-notice').textContent.includes('bindings saved')")

        status, binding = _json_request(
            "GET",
            f"{server.base_url}/v1/studio/projects/drawer-project/worldbooks",
        )
        assert status == 200
        assert binding["project"]["worldbook_ids"] == [book_id]
