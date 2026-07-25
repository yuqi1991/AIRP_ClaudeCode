"""Session Turn Runtime Contract — narrative director execution layer.

These tests drive :class:`SessionTurnRuntime` through its public surface
(``submit`` / ``stop`` / ``events_after`` / ``active_revision`` /
``commit_for_revision`` / ``task`` / ``model_calls_for_task``) and the card's
``chat_log.json`` / projection files. They never assert SQLite table layout or
executor internals. They cover Tickets 03 Slices 1–4:

* Slice 1 — structured TurnDraft + single-write commit tool
* Slice 2 — typed tool allowlist + schema validation + redacted trace events
* Slice 3 — streaming preview + abort
* Slice 4 — ProviderAdapter + FakeProvider + telemetry + secrets
"""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.context_compiler import ContextPolicy
from engine.director import DirectorHandle, NarrativeDirector, ProviderDrivenDirector, ScriptedDirector
from engine.provider import (
    AbortSignal,
    CostEstimate,
    FakeProvider,
    ProviderAborted,
    ProviderAdapter,
    ProviderError,
    ProviderResult,
    RealProviderAdapter,
    UsageRecord,
)
from engine.quality import QualityPolicy
from engine.runtime import SessionTurnRuntime
from engine.tools import ToolRegistry, ToolResult, TOOL_SCHEMAS, validate_draft_dict


# ═══ Fixtures ═══


def write_card_fixture(card_folder, initvar=None):
    (card_folder / ".initvar.json").write_text(
        json.dumps(initvar or {"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")


def write_worldbook(card_folder, entries):
    memory = card_folder / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / ".worldbook_index.json").write_text(
        json.dumps(
            [{"title": t, "section": f"## {t}", "usage": "测试"} for t in entries],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (memory / "reference.md").write_text(
        "\n".join(f"## {t}\n{t}正文。" for t in entries),
        encoding="utf-8",
    )


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


def event_types(runtime, after=0):
    return [event.type for event in runtime.events_after(after)]


# ════════════════════════════════════════════════════════════════════
# Slice 1 — Structured TurnDraft + single-write commit tool
# ════════════════════════════════════════════════════════════════════


def test_director_commits_via_tool_produces_one_turn(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("preview", "<p>海风"),
        ("preview", "掠过礁石。</p>"),
        ("tool", "commit_turn_draft", {
            "draft": build_draft(),
            "expected_revision": 0,
        }),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert result.revision == 1
    assert result.commit_id

    # commit tool returned a successful structured result
    assert director.last_result.ok is True
    assert director.last_result.value["revision"] == 1
    assert director.last_result.value["reused"] is False

    # committed content/summary/options match what the director supplied
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["user"] == "我走向礁石"            # polished_input used
    assert log[0]["summary"] == "玩家来到海边"
    content_js = (tmp_path / "projection" / "content.js").read_text(encoding="utf-8")
    assert "海风掠过礁石" in content_js
    assert "继续观察海面" in content_js

    # MVU commands applied to the committed state_snapshots row
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        row = connection.execute(
            "SELECT state_json FROM state_snapshots WHERE revision = 1"
        ).fetchone()
    state = json.loads(row[0])
    assert state["世界"]["时间"] == "1月1日 10:00"

    # the original six contract events remain, in order, surrounding the new
    # tool/preview events
    types = event_types(runtime)
    for required in [
        "player_message.submitted",
        "task.queued",
        "context.compiled",
        "task.running",
        "narrative.preview.delta",
        "tool_run.started",
        "tool_run.finished",
        "turn.committed",
        "task.succeeded",
    ]:
        assert required in types
    assert types.index("context.compiled") < types.index("turn.committed")
    assert types.index("turn.committed") < types.index("task.succeeded")
    assert runtime.active_revision() == 1
    assert runtime.task(result.task_id).status == "succeeded"


def test_director_that_never_commits_reaches_terminal_non_committed_state(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([("preview", "<p>半生成的文字</p>")])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "failed_terminal"
    assert result.commit_id is None
    assert result.revision == 0
    assert runtime.active_revision() == 0

    # no projection / no chat_log entry
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    assert not (tmp_path / "projection" / "content.js").exists()

    types = event_types(runtime)
    assert "turn.committed" not in types
    assert "task.failed_terminal" in types


def test_director_commit_idempotent_for_same_task(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert event_types(runtime).count("turn.committed") == 1
    # the second commit call returned the reused commit, not a new one
    assert director.last_result.ok is True
    assert director.last_result.value["reused"] is True



def test_quality_gate_rejection_allows_same_task_retry_then_commits_once(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {
            "draft": build_draft(content="<p>短</p>", mvu_commands=""),
            "expected_revision": 0,
        }),
        ("tool", "commit_turn_draft", {
            "draft": build_draft(
                content="<p>海风压低浪头，潮水一下一下拍着礁石边的湿沙。</p>",
                mvu_commands="",
            ),
            "expected_revision": 0,
        }),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
        session_settings={"wordCount": 20},
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert result.revision == 1
    assert director.results[0].ok is False
    assert director.results[0].error == "quality_gate_failed"
    assert director.results[1].ok is True
    assert director.results[1].value["reused"] is False
    assert runtime.active_revision() == 1
    assert event_types(runtime).count("turn.committed") == 1
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert "海风压低浪头" in log[0]["ai"]



def test_quality_retry_exhaustion_fails_without_commit_or_projection(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    bad_draft = {"draft": build_draft(content="<p>短</p>", mvu_commands=""), "expected_revision": 0}
    director = ScriptedDirector([
        ("tool", "commit_turn_draft", bad_draft),
        ("tool", "commit_turn_draft", bad_draft),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
        quality_policy=QualityPolicy(min_chars=8, max_chars=200),
        max_commit_validation_retries=2,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "quality_exhausted"
    assert result.commit_id is None
    assert result.revision == 0
    assert runtime.active_revision() == 0
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    assert not (tmp_path / "projection" / "content.js").exists()
    assert director.results[0].error == "quality_gate_failed"
    assert director.results[1].error == "quality_exhausted"
    types = event_types(runtime)
    assert "turn.committed" not in types
    assert "task.quality_exhausted" in types



def test_invalid_mvu_schema_path_rejected_until_corrected_commit(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {
            "draft": build_draft(
                content="<p>浪头扑上来，碎沫打湿了鞋尖。</p>",
                mvu_commands="_.set('世界.不存在字段', '错');",
            ),
            "expected_revision": 0,
        }),
        ("tool", "commit_turn_draft", {
            "draft": build_draft(
                content="<p>浪头扑上来，潮水退回礁石间。</p>",
                mvu_commands="_.set('世界.时间', '1月1日 10:00');",
            ),
            "expected_revision": 0,
        }),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
        quality_policy=QualityPolicy(min_chars=8, max_chars=200),
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert result.revision == 1
    assert director.results[0].ok is False
    assert director.results[0].error == "mvu_validation_failed"
    assert director.results[1].ok is True
    assert runtime.active_revision() == 1
    assert event_types(runtime).count("turn.committed") == 1
    types = event_types(runtime)
    assert "task.mvu_validation_failed" in types
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1



def test_commit_validation_uses_base_revision_snapshot_not_live_projection_files(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    first_director = ScriptedDirector([
        ("tool", "commit_turn_draft", {
            "draft": build_draft(
                content="<p>第一回合里，风从海面吹过来。</p>",
                mvu_commands="_.set('世界.时间', '1月1日 10:00');",
            ),
            "expected_revision": 0,
        }),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=first_director,
        quality_policy=QualityPolicy(min_chars=8, max_chars=200),
    )
    first = runtime.submit(text="第一回合", idempotency_key="submit-1")
    assert first.status == "succeeded"
    assert first.revision == 1

    live_log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    live_log[-1]["variables"]["stat_data"]["世界"]["不存在字段"] = "live-drift"
    (card_folder / "chat_log.json").write_text(json.dumps(live_log, ensure_ascii=False), encoding="utf-8")
    (card_folder / "state.js").write_text("window.__TEST_STATE__ = 'live-drift';", encoding="utf-8")

    second_director = ScriptedDirector([
        ("tool", "commit_turn_draft", {
            "draft": build_draft(
                content="<p>第二回合里，风更大了，潮水漫过台阶。</p>",
                mvu_commands="_.set('世界.不存在字段', '仍然非法');",
            ),
            "expected_revision": 1,
        }),
    ])
    runtime.executor = second_director

    second = runtime.submit(text="第二回合", idempotency_key="submit-2")

    assert second.status == "mvu_validation_failed"
    assert second.commit_id is None
    assert runtime.active_revision() == 1
    assert second_director.results[0].ok is False
    assert second_director.results[0].error == "mvu_validation_failed"
    assert event_types(runtime).count("turn.committed") == 1


# ════════════════════════════════════════════════════════════════════
# Slice 2 — Typed tool allowlist + schema validation + redaction
# ════════════════════════════════════════════════════════════════════


def test_tool_allowlist_is_closed_no_bash_filewrite_or_network():
    allowed = set(ToolRegistry.ALLOWED)
    assert allowed == {
        "get_session_snapshot",
        "get_recent_memory",
        "load_worldbook_entry",
        "validate_state_proposal",
        "commit_turn_draft",
    }
    forbidden_substrings = ("bash", "shell", "exec", "write_file", "filesystem", "http", "fetch", "network", "curl")
    for name in allowed:
        assert not any(substr in name.lower() for substr in forbidden_substrings)


def test_invalid_tool_args_return_stable_error_with_no_mutation(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        # missing required 'draft' and 'expected_revision'
        ("tool", "commit_turn_draft", {}),
        # then commit correctly so the task succeeds and we can introspect
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    bad = director.results[0]
    good = director.results[1]
    assert bad.ok is False
    assert bad.error.startswith("tool_validation_error")
    assert good.ok is True
    # invalid call did not advance revision; only the good commit did
    assert runtime.active_revision() == 1


def test_unknown_tool_name_is_rejected(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "run_bash", {"cmd": "rm -rf /"}),
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert director.results[0].ok is False
    assert director.results[0].error.startswith("unknown_tool")


def test_tool_runs_emit_redacted_trace_events(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    long_secret_in_content = "机密" * 50
    draft = build_draft(content="<p>正文</p>", mvu_commands="")
    draft["content"] = "<p>" + long_secret_in_content + "</p>"

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": draft, "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    events = runtime.events_after(0)
    started = next(e for e in events if e.type == "tool_run.started" and e.payload["tool"] == "commit_turn_draft")
    finished = next(e for e in events if e.type == "tool_run.finished" and e.payload["tool"] == "commit_turn_draft")

    # args_hash present, redacted args present
    assert len(started.payload["args_hash"]) == 64
    assert isinstance(started.payload["args"], dict)
    # long content + secrets never appear verbatim in the trace
    serialized = json.dumps([e.payload for e in events], ensure_ascii=False)
    assert long_secret_in_content not in serialized
    assert "duration_ms" in finished.payload
    assert isinstance(finished.payload["duration_ms"], int)


def test_validate_state_proposal_runs_no_mutation(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    proposal = [{"op": "replace", "path": "/世界/时间", "value": "1月1日 23:00"}]
    director = ScriptedDirector([
        ("tool", "validate_state_proposal", {"proposal": proposal}),
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    validation = director.results[0]
    assert validation.ok is True
    assert "世界.时间" in validation.value["touched_paths"]
    # the validation call did NOT mutate the baseline state snapshot
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        row = connection.execute(
            "SELECT state_json FROM state_snapshots WHERE revision = 1"
        ).fetchone()
    # the committed snapshot reflects the committed draft's MVU (10:00),
    # proving the validation proposal (23:00) was not applied
    assert json.loads(row[0])["世界"]["时间"] == "1月1日 10:00"


def test_load_worldbook_entry_tool_enforces_policy_limit(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder, initvar={})
    write_worldbook(card_folder, ["条目0", "条目1", "条目2"])

    director = ScriptedDirector([
        ("tool", "load_worldbook_entry", {"title": "条目0", "reason": "first"}),
        ("tool", "load_worldbook_entry", {"title": "条目1", "reason": "second"}),
        ("tool", "load_worldbook_entry", {"title": "条目2", "reason": "third-over-limit"}),
        ("tool", "commit_turn_draft", {"draft": build_draft(content="<p>x</p>"), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
        manifest_policy=ContextPolicy(
            version="limit-v1", token_budget=4000, max_worldbook_loads=2
        ),
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert director.results[0].ok is True
    assert director.results[1].ok is True
    over = director.results[2]
    assert over.ok is False
    assert over.error == "worldbook_load_failed"


# ════════════════════════════════════════════════════════════════════
# Slice 3 — Streaming preview + abort
# ════════════════════════════════════════════════════════════════════


def test_preview_deltas_appear_as_ordered_events(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("preview", "<p>第一段"),
        ("preview", "第二段</p>"),
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    preview_events = [e for e in runtime.events_after(0) if e.type == "narrative.preview.delta"]
    assert [e.payload["preview"] for e in preview_events] == ["<p>第一段", "第二段</p>"]
    assert all(e.payload["length"] == len(e.payload["preview"]) for e in preview_events)
    assert all(len(e.payload["delta_hash"]) == 64 for e in preview_events)


def test_aborted_run_keeps_partial_preview_uncommitted(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    # director emits preview, then blocks on the abort signal, then returns
    # without committing
    started = threading.Event()

    class AbortingDirector(NarrativeDirector):
        def __init__(self):
            self.observed_aborted = False

        def direct(self, handle, compiled):
            handle.emit_preview("<p>半生成的</p>")
            started.set()
            handle.signal.wait(timeout=5)
            self.observed_aborted = handle.aborted
            return  # no commit

    director = AbortingDirector()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    thread = threading.Thread(
        target=runtime.submit, args=("我走向海边", "submit-1")
    )
    thread.start()
    assert started.wait(timeout=2)
    # wait for the task id to be registered for abort
    task_id = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not task_id:
        task_id = runtime.task_id_for_key("submit-1")
        time.sleep(0.01)
    assert task_id is not None
    runtime.stop(task_id)
    thread.join(timeout=5)

    assert director.observed_aborted is True
    assert runtime.active_revision() == 0
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    assert not (tmp_path / "projection" / "content.js").exists()

    types = event_types(runtime)
    assert "narrative.preview.delta" in types
    assert "turn.committed" not in types
    assert "task.cancelled" in types
    assert runtime.task(task_id).status == "cancelled"


def test_abort_after_commit_still_keeps_committed_turn(tmp_path):
    """Abort is safe: once committed, the turn is the player's authoritative fact."""
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
        ("wait_for_aborted",),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    started = threading.Event()

    real_direct = director.direct

    def wrapped(handle, compiled):
        started.set()
        real_direct(handle, compiled)

    director.direct = wrapped

    thread = threading.Thread(target=runtime.submit, args=("我走向海边", "submit-1"))
    thread.start()
    assert started.wait(timeout=2)
    task_id = runtime.task_id_for_key("submit-1")
    runtime.stop(task_id)
    thread.join(timeout=5)

    # committed before abort → turn stands
    assert runtime.active_revision() == 1
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1


def test_stop_marks_queued_task_cancelled(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    # a director that never proceeds past the first yield — used only to create
    # a task row that we then stop before it actually runs
    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    # Seed a task row directly in "queued" state to exercise the queued-cancel
    # branch without engaging the running director.
    task_id = "seed-queued-task"
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, base_revision, source_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "local", "seed-key", "unused", "queued", 0, 0, "{}"),
        )

    assert runtime.stop(task_id) is True
    assert runtime.task(task_id).status == "cancelled"


# ════════════════════════════════════════════════════════════════════
# Slice 4 — ProviderAdapter + FakeProvider + telemetry + secrets
# ════════════════════════════════════════════════════════════════════


def test_provider_adapter_is_a_clean_interface_and_real_adapter_raises():
    # the seam exists and is clearly marked
    assert hasattr(ProviderAdapter, "stream")
    assert hasattr(ProviderAdapter, "model_id")
    real = RealProviderAdapter()
    for method, args in (("stream", (object(), AbortSignal())), ("model_id", ("narrative_director",))):
        try:
            getattr(real, method)(*args)
        except NotImplementedError:
            continue
        raise AssertionError(f"RealProviderAdapter.{method} must raise NotImplementedError")


def test_provider_tool_sequence_commits_via_tool_after_feedback(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    # Round 1: provider requests a (validating) tool call. Round 2: provider
    # emits final text and a commit tool call.
    provider = FakeProvider(
        scripts=[
            [
                {"type": "tool_call", "id": "c1", "name": "get_session_snapshot", "args": {}},
                {"type": "final", "stop_reason": "tool_calls"},
            ],
            [
                {"type": "text", "text": "<p>海风掠过礁石。</p>"},
                {"type": "tool_call", "id": "c2", "name": "commit_turn_draft", "args": {
                    "draft": build_draft(), "expected_revision": 0,
                }},
                {"type": "final", "stop_reason": "tool_calls"},
            ],
        ],
        usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
        rates=CostEstimate(amount=0.012, currency="USD", rate_version="fake-rates-v1"),
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=4, max_retries=1)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert result.revision == 1

    types = event_types(runtime)
    # two model calls happened
    assert types.count("model_call.started") == 2
    assert types.count("model_call.finished") == 2
    # tool dispatch happened, with the snapshot read preceding the commit
    started_tools = [e.payload["tool"] for e in runtime.events_after(0) if e.type == "tool_run.started"]
    assert started_tools == ["get_session_snapshot", "commit_turn_draft"]
    # the snapshot tool actually saw the runtime
    snapshot_finished = next(
        e for e in runtime.events_after(0)
        if e.type == "tool_run.finished" and e.payload["tool"] == "get_session_snapshot"
    )
    assert snapshot_finished.payload["ok"] is True
    # preview delta arrived during round 2
    preview_events = [e for e in runtime.events_after(0) if e.type == "narrative.preview.delta"]
    assert any("海风掠过礁石" in e.payload["preview"] for e in preview_events)


def test_retryable_provider_error_recovers_and_commits(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    provider = FakeProvider(
        scripts=[
            [{"type": "error", "category": "provider_unavailable", "retryable": True, "message": "transient"}],
            [
                {"type": "text", "text": "<p>恢复后正文。</p>"},
                {"type": "tool_call", "id": "c1", "name": "commit_turn_draft", "args": {
                    "draft": build_draft(content="<p>恢复后正文。</p>"), "expected_revision": 0,
                }},
                {"type": "final", "stop_reason": "tool_calls"},
            ],
        ],
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=3, max_retries=3)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert provider.call_count == 2  # first errored, second succeeded
    # one model_call.finished recorded (the retry succeeded within the same call ordinal)
    calls = runtime.model_calls_for_task(result.task_id)
    assert len(calls) == 1
    assert calls[0]["stop_reason"] == "tool_calls"


def test_terminal_provider_error_fails_without_commit(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    provider = FakeProvider(
        scripts=[
            [{"type": "error", "category": "provider_rejected", "retryable": False, "message": "policy"}],
        ],
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=3, max_retries=2)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "failed_terminal"
    assert result.commit_id is None
    assert runtime.active_revision() == 0
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    types = event_types(runtime)
    assert "turn.committed" not in types
    assert "task.failed_terminal" in types


def test_abort_during_provider_stream_cancels_without_commit(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    provider = FakeProvider(
        scripts=[
            [
                {"type": "text", "text": "<p>开始生成"},
                {"type": "block_until_aborted", "timeout": 5},
            ],
        ],
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=2, max_retries=0)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )

    started = threading.Event()
    real_direct = director.direct

    def wrapped(handle, compiled):
        started.set()
        real_direct(handle, compiled)

    director.direct = wrapped

    thread = threading.Thread(target=runtime.submit, args=("我走向海边", "submit-1"))
    thread.start()
    assert started.wait(timeout=2)
    task_id = runtime.task_id_for_key("submit-1")
    runtime.stop(task_id)
    thread.join(timeout=5)

    assert runtime.active_revision() == 0
    assert runtime.task(task_id).status == "cancelled"
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    types = event_types(runtime)
    assert "turn.committed" not in types
    assert "task.cancelled" in types


def test_model_call_records_usage_latency_stopreason_and_rateversioned_cost(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    provider = FakeProvider(
        scripts=[
            [
                {"type": "tool_call", "id": "c1", "name": "commit_turn_draft", "args": {
                    "draft": build_draft(), "expected_revision": 0,
                }},
                {"type": "final", "stop_reason": "tool_calls",
                 "usage": {"prompt_tokens": 333, "completion_tokens": 99, "total_tokens": 432}},
            ],
        ],
        rates=CostEstimate(amount=0.045, currency="USD", rate_version="catalog-2026-07"),
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=2, max_retries=0)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    calls = runtime.model_calls_for_task(result.task_id)
    assert len(calls) == 1
    call = calls[0]
    assert call["prompt_tokens"] == 333
    assert call["completion_tokens"] == 99
    assert call["total_tokens"] == 432
    assert call["stop_reason"] == "tool_calls"
    assert isinstance(call["latency_ms"], int) and call["latency_ms"] >= 0
    assert call["cost_amount"] == 0.045
    assert call["cost_currency"] == "USD"
    assert call["cost_rate_version"] == "catalog-2026-07"
    # manifest reference recorded
    assert call["manifest_id"]

    finished = next(e for e in runtime.events_after(0) if e.type == "model_call.finished")
    assert finished.payload["usage"]["total_tokens"] == 432
    assert finished.payload["cost_estimate"]["rate_version"] == "catalog-2026-07"


def test_provider_credentials_never_leak_into_traces_or_projections(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    secret_marker = "sk-DO-NOT-LEAK-7f3a9b2e"
    provider = FakeProvider(
        scripts=[
            [
                {"type": "tool_call", "id": "c1", "name": "commit_turn_draft", "args": {
                    "draft": build_draft(), "expected_revision": 0,
                }},
                {"type": "final"},
            ],
        ],
        credentials={"api_key": secret_marker},
    )
    director = ProviderDrivenDirector(provider, max_tool_rounds=2, max_retries=0)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    runtime.submit(text="我走向海边", idempotency_key="submit-1")

    # scan every durable surface for the secret
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event_rows = connection.execute("SELECT type, payload FROM events").fetchall()
    events_blob = json.dumps(
        [dict(r) for r in event_rows], ensure_ascii=False
    )
    assert secret_marker not in events_blob
    # model_calls / manifests / tool traces
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        for table in ("model_calls", "context_manifests", "commits", "state_snapshots"):
            cols = [c[1] for c in connection.execute(f"PRAGMA table_info({table})").fetchall()]
            data = connection.execute(f"SELECT * FROM {table}").fetchall()
            blob = json.dumps([dict(zip(cols, row)) for row in data], ensure_ascii=False)
            assert secret_marker not in blob, f"secret leaked via {table}"
    # projection + chat_log files
    for path in (
        card_folder / "chat_log.json",
        tmp_path / "projection" / "content.js",
        tmp_path / "projection" / "state.js",
    ):
        if path.exists():
            assert secret_marker not in path.read_text(encoding="utf-8")


def test_stale_expected_revision_rejected_via_tool_without_commit(tmp_path):
    """Optimistic-revision contract: a stale commit_turn_draft cannot advance the head."""
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    # first task commits at revision 0 → 1
    first_director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(content="<p>第一回合。</p>"), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=first_director,
    )
    first = runtime.submit(text="第一回合", idempotency_key="s1")
    assert first.revision == 1

    # second task tries to commit against the now-stale revision 0
    second_director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(content="<p>不应写入。</p>"), "expected_revision": 0}),
    ])
    runtime.executor = second_director
    second = runtime.submit(text="第二回合", idempotency_key="s2")

    # stale_revision is the precise terminal non-committed category
    assert second.status in ("stale_revision", "failed_terminal")
    assert second.commit_id is None
    assert second_director.last_result.ok is False
    assert second_director.last_result.error == "stale_revision"
    # head still at 1, only one chat turn
    assert runtime.active_revision() == 1
    assert len(json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))) == 1


def test_cancelled_task_is_not_reanimated_by_submit(tmp_path):
    """Lost-cancel regression: a task a concurrent stop() marked cancelled
    during context compilation must not be silently overwritten to running /
    committed by the overlapping submit. The director must not run."""
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    director = ScriptedDirector([
        ("tool", "commit_turn_draft", {"draft": build_draft(), "expected_revision": 0}),
    ])
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    # Seed a real queued task with a valid source snapshot, then pre-cancel it
    # via stop()'s queued path — simulating a stop() that won the race before
    # submit registered an abort signal.
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, "
            "base_revision, source_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("seeded", "local", "submit-1", "我走向海边", "queued", 0, 0,
             json.dumps({"card_facts": {"name": "x"}}, ensure_ascii=False)),
        )
    assert runtime.stop("seeded") is True
    assert runtime.task("seeded").status == "cancelled"

    result = runtime.submit(text="我走向海边", idempotency_key="submit-1")

    assert result.status == "cancelled"
    assert result.commit_id is None
    assert runtime.active_revision() == 0
    # the director never ran, no projection, no chat turn
    assert director.results == []
    assert json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8")) == []
    assert not (tmp_path / "projection" / "content.js").exists()
    types = event_types(runtime)
    assert "turn.committed" not in types
    assert "task.running" not in types
    assert types.count("task.cancelled") >= 1
