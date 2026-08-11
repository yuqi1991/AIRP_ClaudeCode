from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer

from test_agent_studio_golden_path import _HeadlessBrowser, _connect, _json_request


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "airp" / "web"


def test_game_page_mounts_regex_collection_drawer_and_api_controls():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "regex-drawer.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "regex-drawer.css").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="regex-drawer.css">' in page
    assert '<script src="regex-drawer.js"></script>' in page
    assert 'data-airp-drawer-view="worldbooks"' in page
    assert "document.getElementById('regex-drawer-panel')" in script
    assert "studio:drawer-opened" in script
    for control in (
        "regex-drawer-library-search",
        "regex-drawer-new",
        "regex-drawer-list",
        "regex-drawer-name",
        "regex-drawer-add-rule",
        "regex-drawer-rules",
        "regex-drawer-test-input",
        "regex-drawer-test-target",
        "regex-drawer-test-output",
        "regex-drawer-diagnostics",
        "regex-drawer-copy",
        "regex-drawer-delete",
    ):
        assert control in script
    assert "regex-drawer-agent-bindings" not in script
    for target in ("input", "output", "both"):
        assert "'" + target + "'" in script
    for api in ("/v1/studio/regex-collections", "/copy", "/test"):
        assert api in script
    assert "/v1/studio/agents" not in script
    assert "regex-drawer-body" in styles
    assert "regex-rule-card" in styles
    assert "regex-diagnostic" in styles


@pytest.mark.skipif(
    shutil.which("google-chrome") is None or _connect is None,
    reason="requires google-chrome and websockets",
)
def test_regex_drawer_tests_collection_and_agent_editor_owns_binding(tmp_path: Path):
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
            f"{server.base_url}/v1/studio/regex-collections",
            {
                "id": "drawer-regex",
                "name": "Strip prose",
                "rules": [
                    {
                        "id": "strip-markup",
                        "name": "Strip markup",
                        "enabled": True,
                        "target": "output",
                        "pattern": r"<[^>]+>",
                        "flags": "g",
                        "replacement": "",
                    }
                ],
            },
        )
        assert status == 201
        collection_id = created["collection"]["id"]
        status, _agent = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {"agent_id": "drawer-agent", "name": "Drawer Agent", "instruction": "Write plainly."},
        )
        assert status == 201

        with _HeadlessBrowser(f"{server.base_url}/", tmp_path / "chrome-profile") as browser:
            browser.wait_for('window.AIRPRegexDrawer && window.AIRPStudioAgentsDrawer && document.querySelector("#studio-regex-toggle")')
            browser.click("#studio-regex-toggle")
            browser.wait_for('document.querySelector("[data-regex-drawer-view=\\"regex-collections\\"]")')
            browser.wait_for('document.querySelector("#regex-drawer-list [data-regex-collection-id]")')
            assert browser.evaluate("document.getElementById('regex-drawer-panel').parentElement.id === 'studio-drawer-host'")
            assert browser.evaluate("document.getElementById('studio-drawer-panel').hidden")

            browser.click("#studio-model-toggle")
            browser.wait_for("!document.getElementById('studio-model-view').hidden")
            assert browser.evaluate("document.getElementById('regex-drawer-panel').hidden")
            browser.click("#studio-agents-toggle")
            browser.wait_for("!document.getElementById('studio-agents-view').hidden")
            assert browser.evaluate("document.getElementById('studio-model-view').hidden")
            browser.click("#studio-regex-toggle")
            browser.wait_for("!document.getElementById('regex-drawer-panel').hidden")
            assert browser.evaluate("document.getElementById('studio-drawer-panel').hidden")
            assert browser.evaluate("document.getElementById('regex-drawer-list').textContent.includes('Strip prose')")

            browser.click(f'#regex-drawer-list [data-regex-collection-id="{collection_id}"]')
            browser.wait_for("document.getElementById('regex-drawer-name').value === 'Strip prose'")
            assert browser.evaluate("document.querySelectorAll('#regex-drawer-rules .regex-rule-card').length === 1")
            assert browser.evaluate("document.querySelector('#regex-drawer-rules select').value === 'output'")

            browser.evaluate(
                """(() => {
                    const input = document.getElementById('regex-drawer-test-input');
                    input.value = '<p>Hello</p>';
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    return true;
                })()"""
            )
            browser.click("#regex-drawer-test")
            browser.wait_for("document.getElementById('regex-drawer-test-output').textContent === 'Hello'")
            assert browser.evaluate("document.getElementById('regex-drawer-diagnostics').textContent.includes('Strip markup')")

            assert browser.evaluate("!document.getElementById('regex-drawer-agent-bindings')")
            browser.click("#studio-agents-toggle")
            browser.wait_for("!document.getElementById('studio-agents-view').hidden")
            assert browser.evaluate(
                """(() => {
                    const button = Array.from(document.querySelectorAll('#studio-agent-list button'))
                        .find((candidate) => candidate.textContent.includes('Drawer Agent'));
                    if (!button) return false;
                    button.click();
                    return true;
                })()"""
            )
            browser.wait_for("document.getElementById('studio-agent-regex').value === ''")
            browser.evaluate(
                f"""(() => {{
                    const select = document.getElementById('studio-agent-regex');
                    select.value = {collection_id!r};
                    select.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }})()"""
            )
            browser.click("#studio-agent-form button[type=submit]")
            browser.wait_for("document.getElementById('studio-drawer-status').textContent.includes('Agent 已保存')")

        status, payload = _json_request("GET", f"{server.base_url}/v1/studio/agents/drawer-agent")
        assert status == 200
        assert payload["agent"]["regex_collection_id"] == collection_id
