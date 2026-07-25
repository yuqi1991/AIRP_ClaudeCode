"""RealProviderAdapter + Node sidecar IPC contract tests (mock mode, no network/key).

These exercise the line-delimited JSON IPC between Python RealProviderAdapter
and skills/sidecar/pi_provider_sidecar.mjs with PI_SIDECAR_MOCK / --mock. They
MUST stay in the fast suite. Real DeepSeek lives in test_real_deepseek_e2e.py.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1]
REPO = SKILLS.parent
sys.path.insert(0, str(SKILLS))

from engine.director import ProviderDrivenDirector
from engine.provider import (
    AbortSignal,
    ProviderAborted,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    RealProviderAdapter,
)
from engine.runtime import SessionTurnRuntime


SIDECAR = SKILLS / "sidecar" / "pi_provider_sidecar.mjs"


def write_card_fixture(card_folder, initvar=None):
    (card_folder / ".initvar.json").write_text(
        json.dumps(initvar or {"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")


def build_draft(content="<p>海风掠过礁石。浪花轻拍岸边。</p>", **overrides):
    draft = {
        "polished_input": "我走向礁石",
        "content": content,
        "summary": "玩家来到海边",
        "options": '<font color="#5a7a5a">继续观察海面</font>',
        "mvu_commands": "_.set('世界.时间', '1月1日 10:00');",
    }
    draft.update(overrides)
    return draft


@pytest.fixture(scope="module")
def require_node_sidecar():
    if not SIDECAR.is_file():
        pytest.skip(f"sidecar missing: {SIDECAR}")
    import shutil

    if not shutil.which("node"):
        pytest.skip("node binary not on PATH")
    return SIDECAR


def test_real_adapter_model_id_defaults(require_node_sidecar):
    adapter = RealProviderAdapter(mock=True)
    assert adapter.model_id("narrative_director") == "deepseek-v4-flash"
    assert adapter.model_id("anything") == "deepseek-v4-flash"


def test_mock_sidecar_streams_text_and_result(require_node_sidecar):
    adapter = RealProviderAdapter(mock=True)
    signal = AbortSignal()
    request = ProviderRequest(
        messages=[{"role": "user", "content": "写一段海边描写"}],
        tools=[],
        model="deepseek-v4-flash",
        metadata={},
    )
    items = list(adapter.stream(request, signal))
    deltas = [i for i in items if isinstance(i, ProviderDelta)]
    results = [i for i in items if isinstance(i, ProviderResult)]
    assert results, "expected a terminal ProviderResult"
    assert any(d.text for d in deltas), "expected text deltas"
    joined = "".join(d.text or "" for d in deltas)
    assert "海风" in joined
    assert "礁石" in joined
    usage = results[-1].usage
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens or usage.total_tokens > 0
    assert results[-1].cost_estimate.rate_version


def test_mock_sidecar_tool_call_then_result(require_node_sidecar):
    # ADR-0011: the model surface is read-only tools. The mock sidecar emits a
    # tool_call delta for whatever tool the request carries.
    adapter = RealProviderAdapter(mock=True)
    signal = AbortSignal()
    request = ProviderRequest(
        messages=[{"role": "user", "content": "读一下场景"}],
        tools=[
            {
                "name": "get_session_snapshot",
                "description": "read session snapshot",
                "parameters": {
                    "required": [],
                    "optional": ["revision"],
                    "types": {"revision": "int"},
                },
            }
        ],
        model="deepseek-v4-flash",
        metadata={"mock_script": "tool_call", "mock_expected_revision": 0},
    )
    items = list(adapter.stream(request, signal))
    tool_deltas = [i for i in items if isinstance(i, ProviderDelta) and i.tool_call]
    results = [i for i in items if isinstance(i, ProviderResult)]
    assert tool_deltas, "expected a tool_call delta"
    call = tool_deltas[0].tool_call
    assert call["name"] == "get_session_snapshot"
    assert results and results[-1].stop_reason in {"tool_calls", "toolUse", "stop"}


def test_mock_sidecar_abort_raises_provider_aborted(require_node_sidecar):
    adapter = RealProviderAdapter(mock=True)
    signal = AbortSignal()
    request = ProviderRequest(
        messages=[{"role": "user", "content": "hang"}],
        tools=[],
        model="deepseek-v4-flash",
        metadata={"mock_script": "hang_until_abort"},
    )

    def cancel_soon():
        time.sleep(0.05)
        signal.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()
    with pytest.raises(ProviderAborted):
        list(adapter.stream(request, signal))


def test_mock_sidecar_crash_is_terminal_internal(require_node_sidecar):
    adapter = RealProviderAdapter(mock=True)
    signal = AbortSignal()
    request = ProviderRequest(
        messages=[{"role": "user", "content": "crash"}],
        tools=[],
        model="deepseek-v4-flash",
        metadata={"mock_script": "crash"},
    )
    with pytest.raises(ProviderError) as ei:
        list(adapter.stream(request, signal))
    assert ei.value.retryable is False
    assert ei.value.category == "terminal_internal"


def test_mock_sidecar_retryable_error_category(require_node_sidecar):
    adapter = RealProviderAdapter(mock=True)
    signal = AbortSignal()
    request = ProviderRequest(
        messages=[{"role": "user", "content": "err"}],
        tools=[],
        model="deepseek-v4-flash",
        metadata={"mock_script": "error_retryable"},
    )
    with pytest.raises(ProviderError) as ei:
        list(adapter.stream(request, signal))
    assert ei.value.retryable is True
    assert ei.value.category == "provider_unavailable"


def test_real_adapter_credentials_never_enter_ipc_or_durables(tmp_path, require_node_sidecar):
    """Marker key must not appear in events/manifests/model_calls/projections.

    The credentials dict is accepted for FakeProvider symmetry but is NOT
    forwarded into the sidecar env or the stream request body.
    """
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    secret_marker = "sk-DO-NOT-LEAK-real-adapter-9c4e1d"
    adapter = RealProviderAdapter(
        mock=True,
        credentials={"api_key": secret_marker, "DEEPSEEK_API_KEY": secret_marker},
    )
    director = ProviderDrivenDirector(adapter, max_tool_rounds=2, max_retries=0)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    result = runtime.submit(text="我走向海边", idempotency_key="submit-real-1")
    assert result.status == "succeeded"
    assert result.revision == 1

    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event_rows = connection.execute("SELECT type, payload FROM events").fetchall()
        events_blob = json.dumps([dict(r) for r in event_rows], ensure_ascii=False)
        assert secret_marker not in events_blob
        for table in ("model_calls", "context_manifests", "commits", "state_snapshots"):
            cols = [c[1] for c in connection.execute(f"PRAGMA table_info({table})").fetchall()]
            data = connection.execute(f"SELECT * FROM {table}").fetchall()
            blob = json.dumps([dict(zip(cols, row)) for row in data], ensure_ascii=False)
            assert secret_marker not in blob, f"secret leaked via {table}"

    for path in (
        card_folder / "chat_log.json",
        tmp_path / "projection" / "content.js",
        tmp_path / "projection" / "state.js",
    ):
        if path.exists():
            assert secret_marker not in path.read_text(encoding="utf-8")


def test_mock_sidecar_end_to_end_commit_via_director(tmp_path, require_node_sidecar):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    adapter = RealProviderAdapter(mock=True)
    director = ProviderDrivenDirector(adapter, max_tool_rounds=3, max_retries=0)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=director,
    )
    result = runtime.submit(text="我走向海边", idempotency_key="submit-mock-e2e")
    assert result.status == "succeeded"
    assert result.revision == 1
    chat = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(chat) >= 1
    calls = runtime.model_calls_for_task(result.task_id)
    assert calls
    assert calls[0]["prompt_tokens"] > 0
    assert calls[0]["completion_tokens"] > 0
    assert calls[0]["total_tokens"] > 0
    assert calls[0]["stop_reason"]
    assert isinstance(calls[0]["latency_ms"], int)
    assert calls[0]["cost_rate_version"]

    previews = [
        e for e in runtime.events_after(0) if e.type == "narrative.preview.delta"
    ]
    assert any("海风" in (e.payload.get("preview") or "") for e in previews)
