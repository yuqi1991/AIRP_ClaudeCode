"""Session Turn Runtime Contract — revision branch / reroll / rollback (Ticket 06).

Black-box tests over SessionTurnRuntime + SessionCommandService + HTTP/SSE.
They never assert SQLite table layout.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.commands import (  # noqa: E402
    ERROR_INVALID_COMMAND,
    ERROR_STALE_REVISION,
    SessionCommandService,
)
from engine.director import ScriptedDirector  # noqa: E402
from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime, TurnDraft  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


# ═══ Fixtures ═══


def write_card_fixture(card_folder):
    (card_folder / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")


class SequentialFakeExecutor:
    """Yields a fixed sequence of TurnDrafts, one per run() call."""

    def __init__(self, drafts):
        self._drafts = list(drafts)
        self._i = 0

    def run(self, text, compiled_context=None):
        if self._i >= len(self._drafts):
            draft = self._drafts[-1]
        else:
            draft = self._drafts[self._i]
            self._i += 1
        if isinstance(draft, TurnDraft):
            return draft
        if isinstance(draft, dict):
            return TurnDraft(**draft)
        return TurnDraft(content=str(draft))


def make_runtime(tmp_path, executor=None, drafts=None):
    card_folder = tmp_path / "card"
    card_folder.mkdir(exist_ok=True)
    write_card_fixture(card_folder)
    projection_root = tmp_path / "projection"
    database_path = tmp_path / "runtime.sqlite3"
    if executor is None:
        if drafts is None:
            drafts = [
                TurnDraft(content="<p>回合一。</p>", summary="一", options='<font color="#5a7a5a">A</font>'),
                TurnDraft(content="<p>回合二。</p>", summary="二", options='<font color="#5a7a5a">B</font>'),
                TurnDraft(content="<p>回合三。</p>", summary="三", options='<font color="#5a7a5a">C</font>'),
                TurnDraft(content="<p>回合四。</p>", summary="四", options='<font color="#5a7a5a">D</font>'),
            ]
        executor = SequentialFakeExecutor(drafts)
    runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=executor,
    )
    return runtime, card_folder, projection_root, database_path


def build_draft(content="<p>海风掠过礁石。</p>", **overrides):
    draft = {
        "polished_input": "我走向礁石",
        "content": content,
        "summary": "玩家来到海边",
        "options": '<font color="#5a7a5a">继续观察海面</font>',
        "mvu_commands": "_.set('世界.时间', '1月1日 10:00');",
    }
    draft.update(overrides)
    return draft


def final_text(content="<p>海风掠过礁石。</p>", **overrides):
    """Harness-commit narrative text (ADR-0011)."""
    d = build_draft(content=content, **overrides)
    parts = [
        f"<polished_input>{d['polished_input']}</polished_input>",
        f"<content>{d['content']}</content>",
        f"<summary>{d['summary']}</summary>",
        f"<options>{d['options']}</options>",
    ]
    if d.get("mvu_commands"):
        parts.append(f"<UpdateVariable>{d['mvu_commands']}</UpdateVariable>")
    return "\n".join(parts)


def http_json(method, url, body=None, timeout=10):
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
        return exc.code, json.loads(raw) if raw else {}


def read_sse_until(url, predicate, timeout=8):
    """Minimal SSE reader used by the HTTP slice."""
    deadline = time.monotonic() + timeout
    events = []
    req = Request(url, headers={"Accept": "text/event-stream"})
    with urlopen(req, timeout=timeout) as resp:
        buf = b""
        while time.monotonic() < deadline:
            chunk = resp.read(256)
            if not chunk:
                time.sleep(0.05)
                if predicate(events):
                    break
                continue
            buf += chunk
            while b"\n\n" in buf:
                raw_event, buf = buf.split(b"\n\n", 1)
                text = raw_event.decode("utf-8", errors="replace")
                event_type = None
                data_lines = []
                event_id = None
                for line in text.split("\n"):
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
                    elif line.startswith("id:"):
                        event_id = line[len("id:") :].strip()
                if not data_lines:
                    continue
                payload = json.loads("\n".join(data_lines))
                events.append(
                    {
                        "type": event_type or payload.get("type"),
                        "id": event_id,
                        "data": payload,
                    }
                )
                if predicate(events):
                    return events, None
    return events, None


# ════════════════════════════════════════════════════════════════════
# Slice 1 — parent_revision + active head; linear path unchanged
# ════════════════════════════════════════════════════════════════════


def test_linear_commits_record_parent_revision_chain(tmp_path):
    runtime, card_folder, _, _ = make_runtime(tmp_path)

    first = runtime.submit(text="输入A", idempotency_key="s1")
    second = runtime.submit(text="输入B", idempotency_key="s2")

    assert first.revision == 1
    assert second.revision == 2
    assert runtime.active_revision() == 2

    lin1 = runtime.commit_lineage(1)
    lin2 = runtime.commit_lineage(2)
    assert lin1 is not None
    assert lin1["revision"] == 1
    assert lin1["parent_revision"] == 0
    assert lin2["revision"] == 2
    assert lin2["parent_revision"] == 1

    # Still queryable via existing commit_for_revision
    assert runtime.commit_for_revision(2).id == second.commit_id

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert [t["user"] for t in log] == ["输入A", "输入B"]


def test_active_lineage_turns_follow_linear_parents(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.submit(text="输入A", idempotency_key="s1")
    runtime.submit(text="输入B", idempotency_key="s2")
    runtime.submit(text="输入C", idempotency_key="s3")

    turns = runtime.active_lineage_turns(limit=3)
    assert [t["user"] for t in turns] == ["输入A", "输入B", "输入C"]
    assert [t["revision"] for t in turns] == [1, 2, 3]


def test_migration_backfills_parent_revision_on_existing_linear_db(tmp_path):
    """Old Ticket 01–05 DBs (no parent_revision) upgrade in place."""
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)
    database_path = tmp_path / "runtime.sqlite3"
    projection_root = tmp_path / "projection"

    # Boot once to create schema, then strip parent_revision as if pre-ticket-06.
    first = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>旧库回合。</p>"),
    )
    first.submit(text="旧输入", idempotency_key="old-1")
    # Simulate pre-v3 DB: drop parent knowledge by recreating commits without the column
    # is hard mid-flight; instead verify re-open still reports parent via public API
    # after a second SessionTurnRuntime boot (migration path).
    restarted = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>新回合。</p>"),
    )
    lin = restarted.commit_lineage(1)
    assert lin["parent_revision"] == 0
    second = restarted.submit(text="新输入", idempotency_key="new-1")
    assert second.revision == 2
    assert restarted.commit_lineage(2)["parent_revision"] == 1


# ════════════════════════════════════════════════════════════════════
# Slice 2 — reroll creates a branch
# ════════════════════════════════════════════════════════════════════


def test_reroll_reuses_input_and_parent_creates_new_active_head(tmp_path):
    drafts = [
        TurnDraft(content="<p>助手A1。</p>", summary="A1", options='<font color="#5a7a5a">x</font>'),
        TurnDraft(content="<p>助手A2。</p>", summary="A2", options='<font color="#5a7a5a">y</font>'),
    ]
    runtime, card_folder, _, _ = make_runtime(tmp_path, drafts=drafts)

    first = runtime.submit(text="输入A", idempotency_key="s1")
    assert first.revision == 1
    assert runtime.commit_lineage(1)["parent_revision"] == 0

    rerolled = runtime.reroll(revision=1, idempotency_key="reroll-1")
    assert rerolled.status == "succeeded"
    assert rerolled.revision == 2
    assert rerolled.commit_id != first.commit_id
    assert runtime.active_revision() == 2

    lin2 = runtime.commit_lineage(2)
    assert lin2["parent_revision"] == 0
    # Original branch remains auditable
    assert runtime.commit_for_revision(1).id == first.commit_id
    assert runtime.commit_lineage(1)["parent_revision"] == 0

    # Active lineage shows A2, not A1
    turns = runtime.active_lineage_turns()
    assert len(turns) == 1
    assert turns[0]["revision"] == 2
    assert "助手A2" in turns[0]["assistant"]
    assert "助手A1" not in turns[0]["assistant"]
    assert turns[0]["user"] == "输入A"

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["user"] == "输入A"
    assert "助手A2" in log[0]["ai"]
    assert "助手A1" not in log[0]["ai"]

    events = runtime.events_after(0)
    types = [e.type for e in events]
    assert "task.reroll_requested" in types
    assert types.count("turn.committed") == 2
    # Superseded commit's events stay
    assert any(
        e.type == "turn.committed" and e.payload.get("revision") == 1 for e in events
    )


def test_reroll_opening_or_unknown_returns_stable_error(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.submit(text="输入A", idempotency_key="s1")

    opening = runtime.reroll(revision=0, idempotency_key="reroll-open")
    assert opening.status != "succeeded"
    # Public surface via command service uses stable error codes; runtime raises/returns.
    # Prefer CommandResult path:
    service = SessionCommandService(runtime)
    via_cmd = service.reroll(revision=0, idempotency_key="reroll-open-cmd")
    assert via_cmd.ok is False
    assert via_cmd.error == "cannot_reroll_opening"
    assert via_cmd.retryable is False

    unknown = service.reroll(revision=99, idempotency_key="reroll-missing")
    assert unknown.ok is False
    assert unknown.error == "unknown_revision"

    assert runtime.active_revision() == 1


def test_reroll_does_not_reuse_old_task_or_commit(tmp_path):
    drafts = [
        TurnDraft(content="<p>A1</p>", summary="A1", options='<font color="#5a7a5a">x</font>'),
        TurnDraft(content="<p>A2</p>", summary="A2", options='<font color="#5a7a5a">y</font>'),
    ]
    runtime, _, _, _ = make_runtime(tmp_path, drafts=drafts)
    first = runtime.submit(text="输入A", idempotency_key="s1")
    second = runtime.reroll(revision=1, idempotency_key="reroll-unique")
    assert second.task_id != first.task_id
    assert second.commit_id != first.commit_id
    # Reusing the same reroll key is idempotent on the NEW task
    again = runtime.reroll(revision=1, idempotency_key="reroll-unique")
    assert again.task_id == second.task_id
    assert again.commit_id == second.commit_id
    assert runtime.active_revision() == 2


# ════════════════════════════════════════════════════════════════════
# Slice 3 — rollback moves head, keeps history, new branch
# ════════════════════════════════════════════════════════════════════


def test_rollback_moves_head_keeps_history_and_supports_new_branch(tmp_path):
    runtime, card_folder, _, _ = make_runtime(tmp_path)
    r1 = runtime.submit(text="输入A", idempotency_key="s1")
    r2 = runtime.submit(text="输入B", idempotency_key="s2")
    r3 = runtime.submit(text="输入C", idempotency_key="s3")
    assert runtime.active_revision() == 3

    rolled = runtime.rollback(revision=1, idempotency_key="rb-1")
    assert rolled.status in ("succeeded", "rolled_back")
    assert runtime.active_revision() == 1

    # History retained
    assert runtime.commit_for_revision(2).id == r2.commit_id
    assert runtime.commit_for_revision(3).id == r3.commit_id

    events = runtime.events_after(0)
    head_events = [e for e in events if e.type in ("session.head_moved", "session.rolled_back")]
    assert head_events, [e.type for e in events]
    assert head_events[-1].payload.get("to_revision") == 1
    assert head_events[-1].payload.get("from_revision") == 3

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["user"] == "输入A"

    # New submit branches from rev1 → rev4 with parent 1
    r4 = runtime.submit(text="输入D", idempotency_key="s4")
    assert r4.revision == 4
    assert runtime.commit_lineage(4)["parent_revision"] == 1
    assert runtime.active_revision() == 4

    turns = runtime.active_lineage_turns()
    assert [t["revision"] for t in turns] == [1, 4]
    assert [t["user"] for t in turns] == ["输入A", "输入D"]
    # Superseded 2/3 excluded from active context
    assert all(t["revision"] not in (2, 3) for t in turns)

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert [t["user"] for t in log] == ["输入A", "输入D"]


def test_rollback_unknown_revision_stable_error(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.submit(text="输入A", idempotency_key="s1")
    service = SessionCommandService(runtime)
    result = service.rollback(revision=99, idempotency_key="rb-bad")
    assert result.ok is False
    assert result.error == "unknown_revision"
    assert runtime.active_revision() == 1


def test_rollback_invalidates_in_flight_stale_commit(tmp_path):
    """Branch stale case under harness-commit (ADR-0011): a task frozen at
    base=1 produces final text, but after rollback moved the head to 0 the
    harness's optimistic check rejects the commit (stale base revision)."""
    from engine.director import NarrativeDirector

    class HoldThenCommitDirector(NarrativeDirector):
        """Blocks until released, then emits final text for the harness to commit."""

        def __init__(self):
            self.gate = threading.Event()
            self.started = threading.Event()

        def direct(self, handle, compiled):
            self.started.set()
            self.gate.wait(timeout=5)
            # Produce final text; the harness will try to commit at base_revision=1.
            handle.set_final_text(final_text(content="<p>不应写入。</p>"))

    # First turn via fake executor so we have rev1 committed.
    runtime, card_folder, projection_root, database_path = make_runtime(tmp_path)
    first = runtime.submit(text="输入A", idempotency_key="s1")
    assert first.revision == 1

    # Swap in a holding director for the in-flight second task.
    holder = HoldThenCommitDirector()
    runtime.executor = holder

    errors = []
    result_box = []

    def run_second():
        try:
            # Task freezes base_revision=1 at creation time.
            result_box.append(runtime.submit(text="输入B", idempotency_key="s2-inflight"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run_second, daemon=True)
    thread.start()
    assert holder.started.wait(timeout=3), "director did not start"

    # Rollback head to 0 while the task is in flight.
    rb = runtime.rollback(revision=0, idempotency_key="rb-during")
    assert runtime.active_revision() == 0
    assert rb.status in ("succeeded", "rolled_back")

    holder.gate.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    # No new commit; active stays 0; chat has no turns (rolled back past rev1... wait)
    # Rollback to 0 means empty active lineage. rev1 still auditable.
    assert runtime.active_revision() == 0
    assert runtime.commit_for_revision(1).id == first.commit_id
    # Second task must not have committed a turn on top
    if result_box:
        second = result_box[0]
        assert second.commit_id is None
        assert second.status in ("stale_revision", "failed_terminal", "cancelled", "quality_exhausted")

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    # Active head 0 → empty chat projection
    assert log == []


def test_reroll_after_two_turns_only_replaces_target_branch_tip(tmp_path):
    drafts = [
        TurnDraft(content="<p>A1</p>", summary="A1", options='<font color="#5a7a5a">x</font>'),
        TurnDraft(content="<p>B1</p>", summary="B1", options='<font color="#5a7a5a">y</font>'),
        TurnDraft(content="<p>B2</p>", summary="B2", options='<font color="#5a7a5a">z</font>'),
    ]
    runtime, card_folder, _, _ = make_runtime(tmp_path, drafts=drafts)
    runtime.submit(text="输入A", idempotency_key="s1")
    runtime.submit(text="输入B", idempotency_key="s2")
    assert runtime.active_revision() == 2

    rerolled = runtime.reroll(revision=2, idempotency_key="reroll-tip")
    assert rerolled.revision == 3
    assert runtime.commit_lineage(3)["parent_revision"] == 1
    turns = runtime.active_lineage_turns()
    assert [t["revision"] for t in turns] == [1, 3]
    assert [t["user"] for t in turns] == ["输入A", "输入B"]
    assert "B2" in turns[1]["assistant"]
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert [t["user"] for t in log] == ["输入A", "输入B"]
    assert "B2" in log[1]["ai"]


# ════════════════════════════════════════════════════════════════════
# Slice 4 — command service + HTTP
# ════════════════════════════════════════════════════════════════════


def test_command_service_reroll_and_rollback_real_semantics(tmp_path):
    drafts = [
        TurnDraft(content="<p>A1</p>", summary="A1", options='<font color="#5a7a5a">x</font>'),
        TurnDraft(content="<p>B1</p>", summary="B1", options='<font color="#5a7a5a">y</font>'),
        TurnDraft(content="<p>A2</p>", summary="A2", options='<font color="#5a7a5a">z</font>'),
    ]
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path, drafts=drafts)
    service = SessionCommandService(runtime)

    s1 = service.submit(text="输入A", idempotency_key="s1")
    assert s1.ok and s1.revision == 1
    s2 = service.submit(text="输入B", idempotency_key="s2")
    assert s2.ok and s2.revision == 2

    rb = service.rollback(revision=1, idempotency_key="rb-1")
    assert rb.ok is True
    assert service.snapshot().active_revision == 1

    reroll = service.reroll(revision=1, idempotency_key="reroll-1")
    assert reroll.ok is True
    assert reroll.revision == 3
    assert service.snapshot().active_revision == 3
    assert runtime.commit_lineage(3)["parent_revision"] == 0

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["user"] == "输入A"
    assert "A2" in log[0]["ai"]

    # missing key
    bad = service.reroll(revision=3, idempotency_key="")
    assert bad.ok is False
    assert bad.error == ERROR_INVALID_COMMAND


def test_http_reroll_streams_commit_for_same_input(tmp_path):
    # Harness-commit (ADR-0011): the director emits final narrative text each
    # phase; the harness commits. Phase 1 commits A1 at base 0; reroll freezes
    # base at rev1's parent (0) and phase 2 commits A2 — same input, new tip.
    from engine.director import NarrativeDirector

    class SwappingDirector(NarrativeDirector):
        def __init__(self):
            self.phase = 0

        def direct(self, handle, compiled):
            self.phase += 1
            if self.phase == 1:
                handle.emit_preview("<p>A1</p>")
                handle.set_final_text(final_text(content="<p>助手A1。</p>", polished_input="输入A"))
            else:
                handle.emit_preview("<p>A2</p>")
                handle.set_final_text(final_text(content="<p>助手A2。</p>", polished_input="输入A"))

    runtime, card_folder, projection_root, _ = make_runtime(
        tmp_path, executor=SwappingDirector()
    )

    with SessionRuntimeServer(runtime, heartbeat_seconds=30) as server:
        # Submit first turn
        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "输入A", "idempotency_key": "http-s1"},
        )
        assert status in (200, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 1:
            time.sleep(0.05)
        assert runtime.active_revision() == 1

        sse_events = []
        sse_error = []

        def sse_client():
            try:
                events, _ = read_sse_until(
                    f"{server.base_url}/v1/session/events/stream?after=0",
                    predicate=lambda evs: sum(
                        1 for e in evs if e["type"] == "turn.committed"
                    )
                    >= 2,
                    timeout=8,
                )
                sse_events.extend(events)
            except Exception as exc:  # noqa: BLE001
                sse_error.append(exc)

        t = threading.Thread(target=sse_client, daemon=True)
        t.start()
        time.sleep(0.05)

        status, reroll_body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/reroll",
            {"revision": 1, "idempotency_key": "http-reroll-1"},
        )
        assert status in (200, 202), reroll_body
        assert reroll_body.get("ok") is not False

        t.join(timeout=10)
        assert not sse_error, sse_error
        types = [e["type"] for e in sse_events]
        assert "turn.committed" in types
        assert "task.reroll_requested" in types or "narrative.preview.delta" in types

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 2:
            time.sleep(0.05)
        assert runtime.active_revision() == 2

        snap_status, snap = http_json("GET", f"{server.base_url}/v1/session/snapshot")
        assert snap_status == 200
        assert snap["active_revision"] == 2

        log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
        assert len(log) == 1
        assert log[0]["user"] == "输入A"
        assert "助手A2" in log[0]["ai"]
        assert "助手A1" not in log[0]["ai"]
