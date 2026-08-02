"""Deterministic release-qualification checks for the canonical runtime.

The real-provider qualification is opt-in (see ``test_real_deepseek_e2e.py``),
but the lifecycle invariants must stay in default CI.  This test deliberately
uses the same HTTP/SSE server and durable SQLite runtime as the browser path:
twenty turns, two reconnects using ``Last-Event-ID`` and one process restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest

from airp.engine.graph_runtime import GraphRuntime, ExecutionPlanCompiler
from airp.engine.node_runner import ProviderNodeRunner
from airp.engine.provider import CostEstimate, FakeProvider
from airp.engine.provider import OpenAICompatibleProviderAdapter
from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


def _agent(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "name": agent_id.title(),
        "instruction": "Return the requested narrative.",
        "provider_profile_id": "fixture-provider",
        "model_id": "fixture-model",
        "generation": {"temperature": 0.2},
        "advanced": {},
        "tool_allowlist": [],
    }


class _Store:
    def __init__(self, values: dict):
        self.values = values

    def get_project(self, item_id: str):
        return json.loads(json.dumps(self.values[item_id]))

    def get_graph(self, item_id: str):
        return json.loads(json.dumps(self.values[item_id]))

    def get_agent(self, item_id: str):
        return json.loads(json.dumps(self.values[item_id]))


def _runtime(tmp_path: Path, provider: Any) -> SessionTurnRuntime:
    styles = tmp_path / "styles"
    styles.mkdir(exist_ok=True)
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True, exist_ok=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    if not (card / "chat_log.json").exists():
        (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Qualification Card"}', encoding="utf-8")
    project = {"id": "qualification-project", "worldbook_ids": []}
    graph = {
        "id": "qualification-graph",
        "nodes": [{"node_id": "writer", "agent_id": "writer"}],
        "output_node_id": "writer",
    }
    compiler = ExecutionPlanCompiler(
        project_store=_Store({project["id"]: project}),
        graph_store=_Store({graph["id"]: graph}),
        agent_store=_Store({"writer": _agent("writer")}),
    )
    return SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(ProviderNodeRunner(lambda _node: provider)),
        project_id=project["id"],
        execution_graph_id=graph["id"],
        bootstrap_legacy_history=False,
    )


def _provider() -> FakeProvider:
    return FakeProvider(
        [
            {"type": "text", "text": "<content>qualified turn</content><summary>done</summary>"},
            {
                "type": "final",
                "stop_reason": "stop",
                "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            },
        ],
        model="fixture-model",
        rates=CostEstimate(amount=0.001, currency="USD", rate_version="qualification-v1"),
    )


def _read_sse_until(base_url: str, after: int, target: int) -> list[dict]:
    """Read a replay stream opened with Last-Event-ID through ``target``."""
    request = Request(
        f"{base_url}/v1/session/events/stream",
        headers={"Accept": "text/event-stream", "Last-Event-ID": str(after)},
    )
    events: list[dict] = []
    current_id: int | None = None
    current_type: str | None = None
    with urlopen(request, timeout=5) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("id:"):
                try:
                    current_id = int(line[3:].strip())
                except ValueError:
                    current_id = None
            elif line.startswith("event:"):
                current_type = line[6:].strip()
            elif not line and current_id is not None and current_type:
                events.append({"sequence": current_id, "type": current_type})
                if current_id >= target:
                    break
                current_id = None
                current_type = None
    return events


def test_twenty_turn_qualification_survives_sse_reconnect_and_restart(tmp_path):
    """Default CI proves the release lifecycle without external credentials."""
    runtime = _runtime(tmp_path, _provider())
    observed_sequences: list[int] = []
    task_ids: list[str] = []

    with SessionRuntimeServer(runtime, heartbeat_seconds=0.05, poll_interval_seconds=0.005) as server:
        for index in range(10):
            result = runtime.submit(f"turn {index + 1}", f"qualification-{index + 1}")
            assert result is not None
            assert result.status == "succeeded"
            task_ids.append(result.task_id)
            if index == 4:
                replay = runtime.events_after(0)
                target = replay[-1].sequence
                streamed = _read_sse_until(server.base_url, 0, target)
                assert streamed
                assert [event["sequence"] for event in streamed] == sorted(
                    {event["sequence"] for event in streamed}
                )
                observed_sequences.extend(event["sequence"] for event in streamed)

    # A fresh runtime instance is the qualification's process-restart boundary.
    restarted = _runtime(tmp_path, _provider())
    with SessionRuntimeServer(restarted, heartbeat_seconds=0.05, poll_interval_seconds=0.005) as server:
        before_reconnect = max((event.sequence for event in restarted.events_after(0)), default=0)
        for index in range(10, 20):
            result = restarted.submit(f"turn {index + 1}", f"qualification-{index + 1}")
            assert result is not None
            assert result.status == "succeeded"
            task_ids.append(result.task_id)
            if index == 14:
                all_events = restarted.events_after(before_reconnect)
                assert all_events
                target = all_events[-1].sequence
                streamed = _read_sse_until(server.base_url, before_reconnect, target)
                assert streamed
                assert all(event["sequence"] > before_reconnect for event in streamed)
                observed_sequences.extend(event["sequence"] for event in streamed)
                before_reconnect = target

    assert len(task_ids) == 20
    assert restarted.active_revision() == 20
    assert len(json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))) == 20
    assert len(restarted.events_after(0)) > len(observed_sequences)
    all_sequences = [event.sequence for event in restarted.events_after(0)]
    assert all_sequences == sorted(set(all_sequences))

    total_tokens = 0
    for task_id in task_ids:
        calls = restarted.model_calls_for_task(task_id)
        assert len(calls) == 1
        call = calls[0]
        assert call["total_tokens"] == 12
        assert call["stop_reason"] == "stop"
        assert call["latency_ms"] >= 0
        assert call["cost_rate_version"] == "qualification-v1"
        total_tokens += call["total_tokens"]
    assert total_tokens == 240


def test_retryable_provider_failure_is_uncommitted_and_traceable(tmp_path):
    provider = FakeProvider(
        [
            {
                "type": "error",
                "category": "provider_unavailable",
                "retryable": True,
                "message": "temporary upstream outage",
            }
        ],
        model="fixture-model",
    )
    runtime = _runtime(tmp_path, provider)

    result = runtime.submit("temporary failure", "qualification-failure")
    assert result is not None

    assert result.status == "failed_retryable"
    assert runtime.active_revision() == 0
    calls = runtime.model_calls_for_task(result.task_id)
    assert len(calls) == 1
    assert calls[0]["stop_reason"] == "provider_unavailable"
    assert calls[0]["total_tokens"] == 0
    run = runtime.graph_run_detail(runtime.graph_run_id_for_task(result.task_id))
    assert run is not None
    failed_call = run["nodes"][0]["model_calls"][0]
    assert failed_call["status"] == "failed"
    assert failed_call["error"]["retryable"] is True
    events = runtime.events_after(0)
    task_failed = [event for event in events if event.type == "task.failed_retryable"]
    assert task_failed and task_failed[-1].payload["retryable"] is True


@pytest.mark.skipif(
    os.environ.get("AIRP_RUN_REAL_QUALIFICATION") != "1"
    or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set AIRP_RUN_REAL_QUALIFICATION=1 and DEEPSEEK_API_KEY to run the billable route qualification",
)
def test_real_deepseek_twenty_turn_route_qualification(tmp_path):
    """Opt-in release gate for one OpenAI-compatible DeepSeek route.

    The deterministic test above proves the lifecycle on every CI run. This
    test deliberately remains explicit because it performs twenty real model
    calls and is evidence for a configured route, not a claim about all
    providers or models.
    """
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    adapter = OpenAICompatibleProviderAdapter(
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        api_key=os.environ["DEEPSEEK_API_KEY"],
        api_format=os.environ.get("DEEPSEEK_API_FORMAT", "chat_completions"),
        model=model,
    )
    runtime = _runtime(tmp_path, adapter)
    task_ids: list[str] = []
    observed_sequences: list[int] = []

    with SessionRuntimeServer(runtime, heartbeat_seconds=0.05, poll_interval_seconds=0.005) as server:
        for index in range(10):
            result = runtime.submit(
                f"请用中文继续这一幕，第 {index + 1} 回合。",
                f"real-qualification-{index + 1}",
            )
            assert result is not None
            assert result.status == "succeeded"
            task_ids.append(result.task_id)
            if index == 4:
                events = runtime.events_after(0)
                target = events[-1].sequence
                streamed = _read_sse_until(server.base_url, 0, target)
                assert streamed
                observed_sequences.extend(event["sequence"] for event in streamed)

    restarted = _runtime(tmp_path, adapter)
    with SessionRuntimeServer(restarted, heartbeat_seconds=0.05, poll_interval_seconds=0.005) as server:
        before_reconnect = max((event.sequence for event in restarted.events_after(0)), default=0)
        for index in range(10, 20):
            result = restarted.submit(
                f"请用中文继续这一幕，第 {index + 1} 回合。",
                f"real-qualification-{index + 1}",
            )
            assert result is not None
            assert result.status == "succeeded"
            task_ids.append(result.task_id)
            if index == 14:
                events = restarted.events_after(before_reconnect)
                target = events[-1].sequence
                streamed = _read_sse_until(server.base_url, before_reconnect, target)
                assert streamed
                assert all(event["sequence"] > before_reconnect for event in streamed)
                observed_sequences.extend(event["sequence"] for event in streamed)

    assert len(task_ids) == 20
    assert restarted.active_revision() == 20
    assert len(json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))) == 20
    assert observed_sequences == sorted(set(observed_sequences))
    calls = [restarted.model_calls_for_task(task_id)[0] for task_id in task_ids]
    assert all(call["total_tokens"] > 0 for call in calls)
    assert all(call["latency_ms"] >= 0 for call in calls)
    assert all(call["cost_rate_version"] != "unknown" for call in calls)
