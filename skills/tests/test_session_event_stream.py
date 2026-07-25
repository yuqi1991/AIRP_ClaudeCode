"""Session Turn Runtime Contract — command API + SSE event stream (Ticket 04).

Black-box tests over :class:`SessionCommandService` and the parallel
:class:`~runtime_server.SessionRuntimeServer`. They issue the same external
commands a browser would and observe durable events, snapshot, chat_log and
projections. They never assert SQLite table layout.

Covers:
* Slice 1 — library command service (submit / cancel / snapshot / deferred)
* Slice 2 — stdlib HTTP + SSE transport
* Slice 3 — snapshot recovery after "refresh" (new service on same DB)
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
    ERROR_UNKNOWN_TASK,
    SessionCommandService,
)
from engine.director import NarrativeDirector, ScriptedDirector  # noqa: E402
from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


# ═══ Fixtures ═══


def write_card_fixture(card_folder):
    (card_folder / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")


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


def make_runtime(tmp_path, executor=None):
    card_folder = tmp_path / "card"
    card_folder.mkdir(exist_ok=True)
    write_card_fixture(card_folder)
    projection_root = tmp_path / "projection"
    database_path = tmp_path / "runtime.sqlite3"
    runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=executor
        or FakeNarrativeExecutor(
            content="<p>海风掠过礁石。</p>",
            summary="玩家来到海边",
            options='<font color="#5a7a5a">继续观察海面</font>',
        ),
    )
    return runtime, card_folder, projection_root, database_path


def scripted_commit_director(extra_steps=None):
    steps = [
        ("preview", "<p>海风"),
        ("preview", "掠过礁石。</p>"),
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ]
    if extra_steps:
        steps = list(extra_steps) + steps
    return ScriptedDirector(steps)


def event_types(events):
    return [e.type for e in events]


def assert_no_legacy_pending(card_folder, projection_root):
    """New runtime path must never create legacy Claude Code signal files."""
    for root in (card_folder, projection_root):
        assert not (root / "input.txt").exists(), f"input.txt leaked under {root}"
        assert not (root / ".pending").exists(), f".pending leaked under {root}"


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


def read_sse_until(url, predicate, timeout=8.0, max_events=200):
    """Connect to an SSE stream and collect events until predicate(events) or timeout.

    Returns ``(events, last_sequence)``. Disconnects by closing the socket.

    Socket idle timeouts are treated as "no data yet" so a long-lived SSE
    stream can wait for the next durable event without failing the client.
    """
    req = Request(url, headers={"Accept": "text/event-stream"}, method="GET")
    events = []
    last_seq = 0
    deadline = time.monotonic() + timeout
    # Short socket timeout: idle periods between events are expected.
    with urlopen(req, timeout=0.5) as resp:
        assert resp.headers.get_content_type() == "text/event-stream"
        # Prefer the raw socket so we can close promptly on success.
        fp = resp.fp
        sock = getattr(fp, "raw", None)
        if sock is not None and hasattr(sock, "read"):
            reader = sock
        else:
            reader = fp
        buffer = ""
        while time.monotonic() < deadline and len(events) < max_events:
            try:
                chunk = reader.read(256)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                # Some platforms return b'' on timeout-like conditions; keep waiting.
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                continue
            if isinstance(chunk, str):
                buffer += chunk
            else:
                buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event = _parse_sse_block(block)
                if event is None:
                    continue  # comment / ping
                events.append(event)
                last_seq = event["sequence"]
                if predicate(events):
                    return events, last_seq
    return events, last_seq


def _parse_sse_block(block: str):
    event_type = None
    event_id = None
    data_lines = []
    for line in block.splitlines():
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("id:"):
            event_id = line[len("id:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if not data_lines:
        return None
    data = json.loads("\n".join(data_lines))
    sequence = int(event_id) if event_id is not None else int(data.get("sequence", 0))
    return {
        "sequence": sequence,
        "type": event_type or data.get("type"),
        "data": data,
    }


# ════════════════════════════════════════════════════════════════════
# Slice 1 — Command API (library)
# ════════════════════════════════════════════════════════════════════


def test_command_submit_advances_revision_and_emits_progress_chain(tmp_path):
    director = scripted_commit_director()
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path, executor=director)
    service = SessionCommandService(runtime)

    result = service.submit(text="我走向海边", idempotency_key="cmd-submit-1")

    assert result.ok is True
    assert result.status == "succeeded"
    assert result.revision == 1
    assert result.task_id and result.commit_id

    snap = service.snapshot()
    assert snap.active_revision == 1
    assert snap.current_task is not None
    assert snap.current_task.task_id == result.task_id
    assert snap.current_task.status == "succeeded"
    assert snap.last_event_sequence >= 6

    types = event_types(service.events_after(0))
    for required in [
        "player_message.submitted",
        "task.queued",
        "context.compiled",
        "task.running",
        "narrative.preview.delta",
        "turn.committed",
        "task.succeeded",
    ]:
        assert required in types
    assert types.index("task.queued") < types.index("turn.committed")
    assert types.index("turn.committed") < types.index("task.succeeded")

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert_no_legacy_pending(card_folder, projection_root)


def test_command_duplicate_submit_same_idempotency_key_is_one_turn(tmp_path):
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path)
    service = SessionCommandService(runtime)

    first = service.submit(text="我敲门", idempotency_key="dup-1")
    second = service.submit(text="我敲门", idempotency_key="dup-1")

    assert first.ok and second.ok
    assert second.task_id == first.task_id
    assert second.commit_id == first.commit_id
    assert second.revision == first.revision == 1
    assert len(json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))) == 1
    assert event_types(service.events_after(0)).count("turn.committed") == 1
    assert_no_legacy_pending(card_folder, projection_root)


def test_command_cancel_running_task_leaves_preview_uncommitted(tmp_path):
    started = threading.Event()

    class AbortingDirector(NarrativeDirector):
        def __init__(self):
            self.observed_aborted = False

        def direct(self, handle, compiled):
            handle.emit_preview("<p>半生成的</p>")
            started.set()
            handle.signal.wait(timeout=5)
            self.observed_aborted = handle.aborted
            return

    director = AbortingDirector()
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path, executor=director)
    service = SessionCommandService(runtime)

    submit_result_box = {}

    def run_submit():
        submit_result_box["result"] = service.submit(
            text="我走向海边", idempotency_key="cancel-run-1"
        )

    thread = threading.Thread(target=run_submit, daemon=True)
    thread.start()
    assert started.wait(timeout=2)

    task_id = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not task_id:
        task_id = runtime.task_id_for_key("cancel-run-1")
        time.sleep(0.01)
    assert task_id

    cancel = service.cancel(task_id=task_id)
    assert cancel.ok is True
    assert cancel.task_id == task_id
    thread.join(timeout=5)

    final = submit_result_box["result"]
    assert final.status == "cancelled"
    assert final.commit_id is None
    assert service.snapshot().active_revision == 0
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    types = event_types(service.events_after(0))
    assert "narrative.preview.delta" in types
    assert "turn.committed" not in types
    assert "task.cancelled" in types
    assert director.observed_aborted is True
    assert_no_legacy_pending(card_folder, projection_root)


def test_command_cancel_unknown_task_returns_stable_error(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    service = SessionCommandService(runtime)

    result = service.cancel(task_id="does-not-exist")
    assert result.ok is False
    assert result.error == ERROR_UNKNOWN_TASK
    assert result.retryable is False

    by_key = service.cancel(idempotency_key="never-submitted")
    assert by_key.ok is False
    assert by_key.error == ERROR_UNKNOWN_TASK


def test_command_reroll_and_rollback_are_live_on_empty_session(tmp_path):
    """Ticket 06: reroll/rollback are real. Empty session → stable unknown_revision."""
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path)
    service = SessionCommandService(runtime)
    before_rev = service.snapshot().active_revision
    before_events = len(service.events_after(0))

    reroll = service.reroll(revision=1, idempotency_key="reroll-1")
    rollback = service.rollback(revision=1, idempotency_key="rollback-1")

    assert reroll.ok is False
    assert reroll.error == "unknown_revision"
    assert reroll.retryable is False

    assert rollback.ok is False
    assert rollback.error == "unknown_revision"

    assert service.snapshot().active_revision == before_rev
    # No side-effect events on failed unknown_revision
    assert len(service.events_after(0)) == before_events
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    assert_no_legacy_pending(card_folder, projection_root)


def test_command_empty_submit_is_invalid_command(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    service = SessionCommandService(runtime)
    result = service.submit(text="   ", idempotency_key="empty-1")
    assert result.ok is False
    assert result.error == "invalid_command"
    assert result.retryable is False
    assert service.snapshot().active_revision == 0


# ════════════════════════════════════════════════════════════════════
# Slice 2 — SSE / HTTP event stream
# ════════════════════════════════════════════════════════════════════


def test_http_submit_sse_receives_progress_and_commit(tmp_path):
    director = scripted_commit_director()
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path, executor=director)

    with SessionRuntimeServer(runtime, heartbeat_seconds=30) as server:
        # Open SSE first so we catch live events as the worker runs.
        sse_events = []
        sse_error = []

        def sse_client():
            try:
                events, _ = read_sse_until(
                    f"{server.base_url}/v1/session/events/stream?after=0",
                    predicate=lambda evs: any(e["type"] == "task.succeeded" for e in evs),
                    timeout=8,
                )
                sse_events.extend(events)
            except Exception as exc:  # noqa: BLE001 — surface in main thread
                sse_error.append(exc)

        sse_thread = threading.Thread(target=sse_client, daemon=True)
        sse_thread.start()
        time.sleep(0.05)  # let the stream connect

        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "我走向海边", "idempotency_key": "http-submit-1"},
        )
        assert status in (200, 202)
        assert body.get("task_id")
        assert body.get("ok") is not False

        sse_thread.join(timeout=10)
        assert not sse_error, sse_error
        types = [e["type"] for e in sse_events]
        for required in [
            "player_message.submitted",
            "task.queued",
            "context.compiled",
            "task.running",
            "narrative.preview.delta",
            "turn.committed",
            "task.succeeded",
        ]:
            assert required in types, types
        # sequences monotonic and unique
        seqs = [e["sequence"] for e in sse_events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
        # wire format carries sequence + type in data
        for e in sse_events:
            assert e["data"]["sequence"] == e["sequence"]
            assert e["data"]["type"] == e["type"]

        # Wait for background submit to finish projection.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 1:
            time.sleep(0.05)
        assert runtime.active_revision() == 1

        status, snap = http_json("GET", f"{server.base_url}/v1/session/snapshot")
        assert status == 200
        assert snap["active_revision"] == 1
        assert snap["current_task"]["status"] == "succeeded"

        log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
        assert len(log) == 1
        assert_no_legacy_pending(card_folder, projection_root)


def test_sse_reconnect_replays_gap_without_skipping_commit(tmp_path):
    # Slow previews so the first connection can disconnect mid-stream.
    director = ScriptedDirector(
        [
            ("preview", "<p>第一段"),
            ("sleep", 0.3),
            ("preview", "第二段</p>"),
            ("sleep", 0.1),
            ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
        ]
    )
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path, executor=director)

    with SessionRuntimeServer(runtime, heartbeat_seconds=30) as server:
        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "我走向海边", "idempotency_key": "reconnect-1"},
        )
        assert status in (200, 202)
        assert body.get("task_id")

        first_events, last_seq = read_sse_until(
            f"{server.base_url}/v1/session/events/stream?after=0",
            predicate=lambda evs: any(e["type"] == "narrative.preview.delta" for e in evs),
            timeout=5,
        )
        assert any(e["type"] == "narrative.preview.delta" for e in first_events)
        assert last_seq > 0

        # Reconnect from last seen sequence; must receive remaining including commit.
        rest, _ = read_sse_until(
            f"{server.base_url}/v1/session/events/stream?after={last_seq}",
            predicate=lambda evs: any(e["type"] == "task.succeeded" for e in evs),
            timeout=8,
        )
        rest_types = [e["type"] for e in rest]
        assert "turn.committed" in rest_types
        assert "task.succeeded" in rest_types
        # No event from the first connection is re-emitted (sequence > last_seq).
        assert all(e["sequence"] > last_seq for e in rest)
        # Exactly one committed across both segments.
        all_types = [e["type"] for e in first_events] + rest_types
        assert all_types.count("turn.committed") == 1

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 1:
            time.sleep(0.05)
        assert runtime.active_revision() == 1
        assert len(json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))) == 1
        assert_no_legacy_pending(card_folder, projection_root)


def test_http_cancel_while_running_streams_cancelled(tmp_path):
    started = threading.Event()

    class AbortingDirector(NarrativeDirector):
        def direct(self, handle, compiled):
            handle.emit_preview("<p>半生成</p>")
            started.set()
            handle.signal.wait(timeout=5)
            return

    runtime, card_folder, projection_root, _ = make_runtime(
        tmp_path, executor=AbortingDirector()
    )

    with SessionRuntimeServer(runtime, heartbeat_seconds=30) as server:
        sse_box = {"events": [], "error": None}

        def sse_client():
            try:
                events, _ = read_sse_until(
                    f"{server.base_url}/v1/session/events/stream?after=0",
                    predicate=lambda evs: any(e["type"] == "task.cancelled" for e in evs),
                    timeout=8,
                )
                sse_box["events"] = events
            except Exception as exc:  # noqa: BLE001
                sse_box["error"] = exc

        sse_thread = threading.Thread(target=sse_client, daemon=True)
        sse_thread.start()
        time.sleep(0.05)

        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "我走向海边", "idempotency_key": "http-cancel-1"},
        )
        assert status in (200, 202)
        task_id = body.get("task_id")
        assert task_id
        assert started.wait(timeout=3)

        c_status, c_body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/cancel",
            {"task_id": task_id},
        )
        assert c_status == 200
        assert c_body.get("ok") is True

        sse_thread.join(timeout=10)
        assert sse_box["error"] is None
        types = [e["type"] for e in sse_box["events"]]
        assert "task.cancelled" in types
        assert "turn.committed" not in types

        # Drain submit worker.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = runtime.task(task_id)
            if task and task.status == "cancelled":
                break
            time.sleep(0.05)
        assert runtime.task(task_id).status == "cancelled"
        assert runtime.active_revision() == 0
        assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
        assert_no_legacy_pending(card_folder, projection_root)


def test_http_path_never_touches_legacy_pending_files(tmp_path):
    runtime, card_folder, projection_root, _ = make_runtime(tmp_path)
    with SessionRuntimeServer(runtime) as server:
        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "我敲门", "idempotency_key": "no-legacy-1"},
        )
        assert status in (200, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 1:
            time.sleep(0.05)
        assert runtime.active_revision() == 1

        # JSON poll fallback also works and still no legacy files.
        status, payload = http_json(
            "GET", f"{server.base_url}/v1/session/events?after=0"
        )
        assert status == 200
        assert any(e["type"] == "turn.committed" for e in payload["events"])

        # Reroll of the just-committed tip is live (Ticket 06); may 200/202.
        status, reroll = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/reroll",
            {"revision": 1, "idempotency_key": "r1"},
        )
        assert status in (200, 202)
        assert reroll.get("ok") is not False or reroll.get("error") in (
            None,
            "unknown_revision",
            "cannot_reroll_opening",
        )

    assert_no_legacy_pending(card_folder, projection_root)


# ════════════════════════════════════════════════════════════════════
# Slice 3 — Snapshot recovery after "refresh"
# ════════════════════════════════════════════════════════════════════


def test_snapshot_recovery_after_refresh_on_same_database(tmp_path):
    runtime, card_folder, projection_root, database_path = make_runtime(
        tmp_path, executor=scripted_commit_director()
    )
    service = SessionCommandService(runtime)
    result = service.submit(text="我走向海边", idempotency_key="refresh-1")
    assert result.ok and result.revision == 1
    commit_id = result.commit_id
    task_id = result.task_id
    events_before = service.events_after(0)
    content_before = (projection_root / "content.js").read_text(encoding="utf-8")

    # Brand-new runtime + command service bound to the same durable paths.
    restarted = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>should-not-run</p>"),
    )
    recovered = SessionCommandService(restarted)
    snap = recovered.snapshot()

    assert snap.active_revision == 1
    assert snap.current_task is not None
    assert snap.current_task.task_id == task_id
    assert snap.current_task.status == "succeeded"
    assert snap.current_task.commit_id == commit_id

    commit = restarted.commit_for_revision(1)
    assert commit is not None
    assert commit.id == commit_id
    assert commit.task_id == task_id
    assert restarted.projection_checkpoint(commit_id) == "applied"

    events_after = recovered.events_after(0)
    assert [e.type for e in events_after] == [e.type for e in events_before]
    assert [e.sequence for e in events_after] == [e.sequence for e in events_before]
    assert (projection_root / "content.js").read_text(encoding="utf-8") == content_before
    assert len(json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))) == 1
    assert_no_legacy_pending(card_folder, projection_root)


def test_http_snapshot_recovery_matches_library_path(tmp_path):
    runtime, card_folder, projection_root, database_path = make_runtime(tmp_path)
    with SessionRuntimeServer(runtime) as server:
        status, body = http_json(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "我推开门", "idempotency_key": "http-refresh-1"},
        )
        assert status in (200, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.active_revision() < 1:
            time.sleep(0.05)
        status, snap1 = http_json("GET", f"{server.base_url}/v1/session/snapshot")
        assert status == 200
        assert snap1["active_revision"] == 1
        commit_id = snap1["current_task"]["commit_id"]

    # New server process simulation: new runtime on same DB, new HTTP server.
    restarted = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>noop</p>"),
    )
    with SessionRuntimeServer(restarted) as server2:
        status, snap2 = http_json("GET", f"{server2.base_url}/v1/session/snapshot")
        assert status == 200
        assert snap2["active_revision"] == 1
        assert snap2["current_task"]["commit_id"] == commit_id
        status, payload = http_json(
            "GET", f"{server2.base_url}/v1/session/events?after=0"
        )
        assert status == 200
        assert any(e["type"] == "turn.committed" for e in payload["events"])
    assert_no_legacy_pending(card_folder, projection_root)
