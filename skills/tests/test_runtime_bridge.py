"""Browser golden-path bridge contract (Ticket: 接通新 runtime 的浏览器黄金路径).

Black-box tests over :class:`SessionRuntimeServer` extended with the frontend
compat layer: static files from skills/styles/, POST /api/submit → runtime
submit (FakeProvider fast path), GET /api/pending, POST /api/reroll, CORS /
OPTIONS compatibility, and file-backed endpoints including runtime preset /
graph config CRUD. The existing frontend polls content.js/state.js every 3s,
so these tests assert that a submit causes the projection (content.js) to
update within the poll window.

Real DeepSeek is opt-in (skip without DEEPSEEK_API_KEY); the fast suite uses a
scripted director that emits final narrative text (ADR-0011 harness-commit).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.director import ScriptedDirector  # noqa: E402
from engine.runtime import SessionTurnRuntime  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


def _write_card(card_folder, *, with_opening=False):
    card_folder.mkdir(parents=True, exist_ok=True)
    (card_folder / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    if with_opening:
        # A pre-existing opening turn (index 0, AI-only) like handler.append_turn
        # would write — so reroll has a user turn (index 1) to operate on after
        # the first submit.
        pass


def _final_text(content="<p>海风掠过礁石，远处的船笛低鸣。</p>", **kw):
    d = {
        "polished_input": "我走向海边",
        "content": content,
        "summary": "玩家来到海边",
        "options": '<font color="#5a7a5a">继续观察海面</font>',
        "mvu_commands": "_.set('世界.时间', '1月1日 10:00');",
    }
    d.update(kw)
    parts = [f"<polished_input>{d['polished_input']}</polished_input>",
             f"<content>{d['content']}</content>",
             f"<summary>{d['summary']}</summary>",
             f"<options>{d['options']}</options>"]
    if d.get("mvu_commands"):
        parts.append(f"<UpdateVariable>{d['mvu_commands']}</UpdateVariable>")
    return "\n".join(parts)


def _scripted_director():
    return ScriptedDirector([
        ("preview", "<p>海风"),
        ("preview", "掠过礁石。</p>"),
        ("final", _final_text()),
    ])


def _http(method, url, body=None, timeout=10):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ════════════════════════════════════════════════════════════════════
# Static files
# ════════════════════════════════════════════════════════════════════


def test_serves_static_index_html_and_content_js_from_styles(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "index.html").write_text("<html>game</html>", encoding="utf-8")
    (styles / "content.js").write_text('window.CONTENT_HTML = "<p>x</p>";', encoding="utf-8")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        # index.html
        with urlopen(f"{server.base_url}/", timeout=5) as resp:
            assert resp.status == 200
            assert "game" in resp.read().decode("utf-8")
        # content.js
        with urlopen(f"{server.base_url}/content.js", timeout=5) as resp:
            assert resp.status == 200
            assert "CONTENT_HTML" in resp.read().decode("utf-8")


def test_runtime_frontend_uses_same_origin_api_urls_and_runtime_config_ui():
    index_html = (SKILLS / "styles" / "index.html").read_text(encoding="utf-8")
    assert "http://localhost:8765" not in index_html
    assert "Runtime Config" in index_html
    assert "/api/runtime/config" in index_html
    assert "python skills/server.py" not in index_html


# ════════════════════════════════════════════════════════════════════
# /api/submit → runtime submit, projection updates within poll window
# ════════════════════════════════════════════════════════════════════


def test_api_submit_runs_runtime_and_projection_updates(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "content.js").write_text('window.CONTENT_HTML = "";', encoding="utf-8")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/submit",
                             {"text": "我走向海边", "charName": ""})
        assert status == 200
        assert body.get("ok") is True

        # Frontend polls content.js; the runtime's projection must update it.
        def content_ready():
            try:
                js = (styles / "content.js").read_text(encoding="utf-8")
                return "海风掠过礁石" in js
            except OSError:
                return False
        assert _wait_for(content_ready, timeout=10), "projection did not update content.js"
        assert runtime.active_revision() == 1


def test_api_submit_returns_task_identity_and_snapshot(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/submit",
                             {"text": "我走向海边", "charName": "玩家"})
        assert status == 200
        assert body["ok"] is True
        assert body["task_id"]
        assert body["status"] in {"queued", "running", "projection_pending", "succeeded"}
        assert body["submitted_text"] == "【玩家】我走向海边"
        assert body["snapshot"]["session_id"] == runtime.session_id
        assert body["snapshot"]["status"] in {"queued", "running", "projection_pending", "succeeded"}


def test_api_submit_rejects_empty_input(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/submit", {"text": "   "})
        assert status == 400
        assert body.get("ok") is False


# ════════════════════════════════════════════════════════════════════
# /api/pending, /api/reroll
# ════════════════════════════════════════════════════════════════════


def test_api_pending_reports_running_then_idle(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("GET", f"{server.base_url}/api/pending")
        assert status == 200
        assert body == {
            "ok": True,
            "initialized": True,
            "pending": False,
            "status": "idle",
            "task_id": None,
            "snapshot": {
                "session_id": runtime.session_id,
                "active_revision": 0,
                "last_event_sequence": 0,
                "current_task": None,
                "status": "idle",
                "pending": False,
            },
        }


def test_api_session_status_snapshot_and_options_are_consistent(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        _http("POST", f"{server.base_url}/api/submit", {"text": "我走向海边"})
        assert _wait_for(lambda: runtime.active_revision() >= 1, timeout=10)

        status, session_status = _http("GET", f"{server.base_url}/api/session_status")
        assert status == 200
        assert session_status["initialized"] is True
        assert session_status["snapshot"]["active_revision"] == 1
        assert session_status["snapshot"]["session_id"] == runtime.session_id
        assert session_status["status"] == session_status["snapshot"]["status"]

        status, snapshot = _http("GET", f"{server.base_url}/api/session_snapshot")
        assert status == 200
        assert snapshot["active_revision"] == session_status["snapshot"]["active_revision"]
        assert snapshot["current_task"]["task_id"] == session_status["snapshot"]["current_task"]["task_id"]

        req = Request(f"{server.base_url}/api/submit", method="OPTIONS")
        with urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            assert "PUT" in resp.headers["Access-Control-Allow-Methods"]


def test_api_reroll_replaces_last_assistant_turn(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    directors = [_scripted_director()]

    class _SwapExecutor:
        # SessionTurnRuntime stores executor; we swap .executor between calls.
        pass

    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=directors[0],
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        # First turn.
        _http("POST", f"{server.base_url}/api/submit", {"text": "我走向海边"})
        assert _wait_for(lambda: runtime.active_revision() >= 1, timeout=10)

        # Reroll with a fresh director producing different text.
        runtime.executor = ScriptedDirector([("final", _final_text(content="<p>浪头退去，礁石裸露。</p>"))])
        status, body = _http("POST", f"{server.base_url}/api/reroll")
        assert status == 200
        assert body.get("ok") is True

        def rerolled():
            try:
                return "浪头退去" in (styles / "content.js").read_text(encoding="utf-8")
            except OSError:
                return False
        assert _wait_for(rerolled, timeout=10), "reroll did not replace content.js"


# ════════════════════════════════════════════════════════════════════
# File-backed endpoints
# ════════════════════════════════════════════════════════════════════


def test_api_openings_and_settings_served_from_styles(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    (styles / "openings.json").write_text(json.dumps([{"id": 0, "title": "默认"}], ensure_ascii=False), encoding="utf-8")
    (styles / "settings.json").write_text(json.dumps({"style": "北棱特调", "wordCount": 600}, ensure_ascii=False), encoding="utf-8")
    card = tmp_path / "card"; _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, openings = _http("GET", f"{server.base_url}/api/openings")
        assert status == 200
        assert isinstance(openings, list) and openings[0]["title"] == "默认"

        status, settings = _http("GET", f"{server.base_url}/api/settings")
        assert status == 200
        assert settings["style"] == "北棱特调"


def test_api_runtime_config_crud_is_file_backed_and_validated(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    presets = styles / "presets"; presets.mkdir()
    graphs = tmp_path / "graphs"; graphs.mkdir()
    (styles / "settings.json").write_text(
        json.dumps({"runtime": {"preset_id": "default", "graph_id": "main"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    preset_data = {
        "id": "default",
        "entries": [
            {
                "id": "policy",
                "kind": "narrative_policy",
                "role": "system",
                "content": "policy",
            },
            {
                "id": "input",
                "kind": "player_input",
                "role": "user",
                "content": "{{player_input}}",
            },
        ],
    }
    graph_data = {
        "id": "main",
        "mode": "sequential",
        "nodes": [
            {
                "id": "director",
                "role": "narrative_director",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
            }
        ],
    }
    (presets / "default.json").write_text(json.dumps(preset_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (graphs / "main.json").write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    card = tmp_path / "card"; _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles, preset_root=presets, graph_root=graphs) as server:
        status, config = _http("GET", f"{server.base_url}/api/runtime/config")
        assert status == 200
        assert config["selected"] == {"preset_id": "default", "graph_id": "main"}
        assert config["presets"][0]["id"] == "default"
        assert config["graphs"][0]["id"] == "main"

        status, preset = _http("GET", f"{server.base_url}/api/runtime/presets/default")
        assert status == 200
        assert preset["data"] == preset_data

        status, graph = _http("GET", f"{server.base_url}/api/runtime/graphs/main")
        assert status == 200
        assert graph["data"] == graph_data

        updated_preset = {**preset_data, "version": "2"}
        status, saved = _http("PUT", f"{server.base_url}/api/runtime/presets/default",
                              {"data": updated_preset})
        assert status == 200
        assert saved["saved"] is True
        assert json.loads((presets / "default.json").read_text(encoding="utf-8")) == updated_preset

        status, rejected = _http(
            "PUT",
            f"{server.base_url}/api/runtime/presets/default",
            {"data": {**updated_preset, "entries": [{"id": "bad", "enabled": "false", "content": "x"}]}},
        )
        assert status == 400
        assert rejected["error"] == "invalid_runtime_config"
        assert rejected["path"] == "entries.0.enabled"
        assert json.loads((presets / "default.json").read_text(encoding="utf-8")) == updated_preset

        status, selected = _http("PUT", f"{server.base_url}/api/runtime/config",
                                 {"preset_id": "default", "graph_id": "main"})
        assert status == 200
        assert selected["runtime"] == {"preset_id": "default", "graph_id": "main"}

        status, bad = _http("PUT", f"{server.base_url}/api/runtime/graphs/-bad",
                            {"text": '{"nodes": []}'})
        assert status == 400
        assert bad["error"] == "invalid_config_id"

def test_api_openings_fall_back_to_card_local_store_and_switch_opening(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    memory = card / "memory"
    memory.mkdir()
    openings = [
        {"id": 0, "title": "默认", "content": "<p>晨雾还没散。</p>", "summary": "默认开场"},
        {"id": 1, "title": "备用", "content": "<p>浪花拍上岸沿。</p>", "summary": "备用开场"},
    ]
    (memory / "openings.json").write_text(json.dumps(openings, ensure_ascii=False, indent=2), encoding="utf-8")
    (card / "chat_log.json").write_text(
        json.dumps([
            {"index": 0, "ai": "<content>\n<p>晨雾还没散。</p>\n</content>\n\n<summary>默认开场</summary>", "summary": "默认开场"}
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, payload = _http("GET", f"{server.base_url}/api/openings")
        assert status == 200
        assert [item["title"] for item in payload] == ["默认", "备用"]

        status, body = _http("POST", f"{server.base_url}/api/switch_opening", {"opening_id": 1})
        assert status == 200
        assert body.get("ok") is True
        log = json.loads((card / "chat_log.json").read_text(encoding="utf-8"))
        assert "浪花拍上岸沿" in log[0]["ai"]

def test_api_delete_turns_from_index_zero_rolls_back_to_opening_revision(tmp_path):
    styles = tmp_path / "styles"; styles.mkdir()
    card = tmp_path / "card"; _write_card(card)
    (card / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00", "地点": "港口"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card / "chat_log.json").write_text(
        json.dumps([
            {"index": 0, "ai": "<content>\n<p>晨雾压着港口。</p>\n</content>\n\n<summary>开场</summary>", "summary": "开场"},
            {"index": 1, "user": "我走向码头", "ai": "<p>木栈道在脚下轻响。</p>", "summary": "来到码头", "variables": {"stat_data": {"世界": {"时间": "1月1日 10:00", "地点": "码头"}}}}
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=_scripted_director(),
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/delete_turns", {"from_index": 0})
        assert status == 200
        assert body.get("ok") is True
        assert runtime.active_revision() == 0
        log = json.loads((card / "chat_log.json").read_text(encoding="utf-8"))
        assert len(log) == 1
        assert "晨雾压着港口" in log[0]["ai"]


# ════════════════════════════════════════════════════════════════════
# Opt-in real DeepSeek smoke (manual/browser)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"),
                    reason="DEEPSEEK_API_KEY not set; real DeepSeek bridge smoke is opt-in")
def test_real_deepseek_bridge_submit_updates_projection(tmp_path):
    from engine.director import ProviderDrivenDirector
    from engine.provider import RealProviderAdapter

    styles = tmp_path / "styles"; styles.mkdir()
    (styles / "content.js").write_text('window.CONTENT_HTML = "";', encoding="utf-8")
    card = tmp_path / "card"; _write_card(card)
    adapter = RealProviderAdapter(mock=False, model="deepseek-v4-flash",
                                  base_url="https://api.deepseek.com")
    director = ProviderDrivenDirector(adapter, max_tool_rounds=8, max_retries=2)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=director,
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/submit",
                             {"text": "请用两三句中文描写清晨的海边，并用 <summary> 给一句摘要、<options> 给一个选项。"},
                             timeout=60)
        assert status == 200 and body.get("ok") is True
        assert _wait_for(lambda: "CONTENT_HTML" in (styles / "content.js").read_text(encoding="utf-8")
                         and len((styles / "content.js").read_text(encoding="utf-8")) > 50,
                         timeout=90), "DeepSeek did not update projection"


# ════════════════════════════════════════════════════════════════════
# Opt-in real DeepSeek smoke (manual/browser)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"),
                    reason="DEEPSEEK_API_KEY not set; real DeepSeek bridge smoke is opt-in")
def test_real_deepseek_bridge_submit_updates_projection(tmp_path):
    from engine.director import ProviderDrivenDirector
    from engine.provider import RealProviderAdapter

    styles = tmp_path / "styles"; styles.mkdir()
    (styles / "content.js").write_text('window.CONTENT_HTML = "";', encoding="utf-8")
    card = tmp_path / "card"; _write_card(card)
    adapter = RealProviderAdapter(mock=False, model="deepseek-v4-flash",
                                  base_url="https://api.deepseek.com")
    director = ProviderDrivenDirector(adapter, max_tool_rounds=8, max_retries=2)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "r.sqlite3", card_folder=card,
        projection_root=styles, executor=director,
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, body = _http("POST", f"{server.base_url}/api/submit",
                             {"text": "请用两三句中文描写清晨的海边，并用 <summary> 给一句摘要、<options> 给一个选项。"},
                             timeout=60)
        assert status == 200 and body.get("ok") is True
        assert _wait_for(lambda: "CONTENT_HTML" in (styles / "content.js").read_text(encoding="utf-8")
                         and len((styles / "content.js").read_text(encoding="utf-8")) > 50,
                         timeout=90), "DeepSeek did not update projection"
