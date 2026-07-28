"""Multi-session save management over one imported card."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime, TurnDraft  # noqa: E402
from engine.session_manager import SessionManager, SessionManagerError  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


def _http(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _build_manager(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    projection = tmp_path / "styles"
    projection.mkdir()
    database = card / ".runtime.sqlite3"

    def factory(session_id, *, bootstrap_legacy_history=False):
        return SessionTurnRuntime(
            database_path=database,
            card_folder=card,
            projection_root=projection,
            executor=FakeNarrativeExecutor(
                content=f"<p>{session_id} 的叙事。</p>",
                summary=f"{session_id} 摘要",
            ),
            session_id=session_id,
            bootstrap_legacy_history=bootstrap_legacy_history,
        )

    runtime = factory("local")
    opening = {"index": 0, "ai": "<p>刻晴站在璃月港。</p>", "summary": "璃月开场"}
    runtime.set_opening_turn(opening, event_type="session.opening_delivered")
    runtime.resume_projection()
    manager = SessionManager(runtime, factory, default_opening=opening)
    return manager, card, projection, factory


def test_sessions_keep_independent_lineage_and_projection(tmp_path):
    manager, card, projection, _ = _build_manager(tmp_path)

    assert [item["id"] for item in manager.list_sessions()] == ["local"]
    branch = manager.create_session("玉京台支线")
    branch_id = branch["id"]
    assert branch["title"] == "玉京台支线"
    assert manager.active_session_id == branch_id
    assert manager.runtime.active_revision() == 0
    assert "刻晴站在璃月港" in (card / "chat_log.json").read_text(encoding="utf-8")

    branch_turn = manager.runtime.submit("我跟刻晴前往玉京台", "shared-turn-key")
    assert branch_turn.status == "succeeded"
    assert manager.runtime.active_revision() == 1

    manager.switch_session("local")
    assert manager.runtime.active_revision() == 0
    local_log = (card / "chat_log.json").read_text(encoding="utf-8")
    assert "刻晴站在璃月港" in local_log
    assert "前往玉京台" not in local_log

    local_turn = manager.runtime.submit("我留在璃月港整理卷宗", "shared-turn-key")
    assert local_turn.status == "succeeded"
    assert manager.runtime.active_revision() == 1
    assert "整理卷宗" in (card / "chat_log.json").read_text(encoding="utf-8")
    manager.switch_session(branch_id)
    branch_log = (projection / "content.js").read_text(encoding="utf-8")
    assert "前往玉京台" in branch_log
    assert "整理卷宗" not in branch_log

    renamed = manager.rename_session(branch_id, "刻晴主线")
    assert renamed["title"] == "刻晴主线"
    manager.delete_session("local")
    sessions = manager.list_sessions()
    assert [item["id"] for item in sessions] == [branch_id]
    assert sessions[0]["active"] is True


def test_http_session_crud_switches_the_active_runtime(tmp_path):
    manager, _, projection, _ = _build_manager(tmp_path)
    with SessionRuntimeServer(
        manager.runtime,
        static_root=projection,
        session_manager=manager,
    ) as server:
        status, listed = _http("GET", f"{server.base_url}/api/sessions")
        assert status == 200
        assert listed["active_session_id"] == "local"

        status, created = _http(
            "POST", f"{server.base_url}/api/sessions", {"title": "新冒险"}
        )
        assert status == 201
        created_id = created["session"]["id"]
        assert created["active_session_id"] == created_id
        assert created["snapshot"]["active_revision"] == 0

        status, renamed = _http(
            "POST",
            f"{server.base_url}/api/sessions/rename",
            {"session_id": created_id, "title": "璃月事务"},
        )
        assert status == 200
        assert renamed["session"]["title"] == "璃月事务"

        status, switched = _http(
            "POST",
            f"{server.base_url}/api/sessions/switch",
            {"session_id": "local"},
        )
        assert status == 200
        assert switched["active_session_id"] == "local"
        assert switched["snapshot"]["session_id"] == "local"

        status, deleted = _http(
            "DELETE", f"{server.base_url}/api/sessions/{created_id}"
        )
        assert status == 200
        assert deleted["deleted_session_id"] == created_id
        assert [item["id"] for item in deleted["sessions"]] == ["local"]


def test_active_session_and_titles_survive_manager_restart(tmp_path):
    manager, card, projection, factory = _build_manager(tmp_path)
    created = manager.create_session("刻晴长期主线")
    session_id = created["id"]
    manager.runtime.submit("我陪刻晴巡视璃月港", "restart-turn")

    restored_id = SessionManager.load_active_session_id(card / ".runtime.sqlite3")
    restored_runtime = factory(restored_id, bootstrap_legacy_history=False)
    restored_runtime.resume_projection()
    restored = SessionManager(restored_runtime, factory)

    assert restored.active_session_id == session_id
    session = next(item for item in restored.list_sessions() if item["id"] == session_id)
    assert session["title"] == "刻晴长期主线"
    assert session["active_revision"] == 1
    assert "巡视璃月港" in (projection / "content.js").read_text(encoding="utf-8")


def test_deleting_active_session_selects_and_projects_a_replacement(tmp_path):
    manager, card, _, _ = _build_manager(tmp_path)
    created = manager.create_session("临时支线")
    manager.runtime.submit("我前往临时支线", "temporary-turn")

    result = manager.delete_session(created["id"])

    assert result["deleted_session_id"] == created["id"]
    assert manager.active_session_id == "local"
    assert manager.runtime.active_revision() == 0
    log = (card / "chat_log.json").read_text(encoding="utf-8")
    assert "刻晴站在璃月港" in log
    assert "临时支线" not in log


def test_switch_and_delete_are_rejected_while_generation_is_active(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingExecutor:
        def run(self, text, compiled_context=None):
            started.set()
            assert release.wait(timeout=5)
            return TurnDraft(content="<p>生成完成。</p>")

    manager, _, _, _ = _build_manager(tmp_path)
    other = manager.create_session("并行保护测试")
    manager.switch_session("local")
    manager.runtime.executor = BlockingExecutor()
    worker = threading.Thread(
        target=manager.runtime.submit,
        args=("执行耗时任务", "busy-session-test"),
    )
    worker.start()
    assert started.wait(timeout=5)

    try:
        for operation in (
            lambda: manager.switch_session(other["id"]),
            lambda: manager.delete_session(other["id"]),
        ):
            try:
                operation()
            except SessionManagerError as exc:
                assert exc.code == "generation_active"
            else:
                raise AssertionError("session mutation must fail during generation")
        assert manager.active_session_id == "local"
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()


def test_stale_active_session_pointer_falls_back_to_local(tmp_path):
    _, card, _, _ = _build_manager(tmp_path)
    with sqlite3.connect(card / ".runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE runtime_metadata SET value = 'missing' WHERE key = 'active_session_id'"
        )

    assert SessionManager.load_active_session_id(card / ".runtime.sqlite3") == "local"
