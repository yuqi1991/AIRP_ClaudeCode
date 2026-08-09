from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from airp.engine.agent_definitions import AgentDefinitionStore
from airp.engine.graph_runtime import ExecutionPlanCompiler, GraphRuntime
from airp.engine.pi_node_runner import PiCoreNodeRunner
from airp.engine.regex_collections import RegexCollectionLibrary
from airp.host.rp.session_runtime import SessionTurnRuntime


class _Store:
    def __init__(self, values: dict[str, dict]):
        self.values = values

    def get_project(self, item_id: str) -> dict:
        return json.loads(json.dumps(self.values[item_id]))

    def get_graph(self, item_id: str) -> dict:
        return json.loads(json.dumps(self.values[item_id]))


class _DeterministicSSEProvider:
    """OpenAI-compatible boundary fixture used by the packaged Pi sidecar."""

    outputs = (
        "draft one",
        "review one",
        "draft two",
        "review two",
        "<content>The harbor bells answer.</content>",
        "draft three",
        "review three",
        "draft four",
        "review four",
        "<content>The lanterns guide the return.</content>",
    )

    def __init__(self):
        self.requests: list[dict] = []
        self.success_count = 0
        self.fail_next = False
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                return

            def do_POST(self):  # noqa: N802
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer local-test-key"
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append(request)
                if provider.fail_next:
                    provider.fail_next = False
                    payload = json.dumps({"error": {"message": "temporary local failure"}}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                output = provider.outputs[provider.success_count]
                provider.success_count += 1
                events = (
                    {"choices": [{"delta": {"content": output}, "finish_reason": "stop"}]},
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                        },
                    },
                )
                payload = "".join(
                    f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events
                ) + "data: [DONE]\n\n"
                encoded = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _runtime(tmp_path: Path, provider: _DeterministicSSEProvider):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Pi Golden Path"}', encoding="utf-8")

    regexes = RegexCollectionLibrary(styles)
    regexes.create_collection(
        {
            "id": "extract-content",
            "name": "Extract Content",
            "rules": [
                {
                    "id": "unwrap",
                    "name": "Unwrap content",
                    "enabled": True,
                    "target": "output",
                    "pattern": "^<content>([\\s\\S]*)</content>$",
                    "flags": "",
                    "replacement": "$1",
                }
            ],
        }
    )
    agents = AgentDefinitionStore(styles)
    common = {
        "provider_profile_id": "local-provider",
        "model_id": "local-model",
        "generation": {"temperature": 0},
        "tool_allowlist": [],
    }
    agents.create_agent(
        {
            **common,
            "agent_id": "draft-writer",
            "name": "Draft Writer",
            "instruction": "Draft from stable history: {{recent_turns}}",
        }
    )
    agents.create_agent(
        {
            **common,
            "agent_id": "reviewer",
            "name": "Reviewer",
            "instruction": "Review the handoff.",
        }
    )
    agents.create_agent(
        {
            **common,
            "agent_id": "final-writer",
            "name": "Final Writer",
            "instruction": "Deliver from stable history: {{recent_turns}}",
            "regex_collection_id": "extract-content",
        }
    )
    project = {"id": "project", "worldbook_ids": []}
    graph = {
        "id": "writing",
        "mode": "handoff",
        "nodes": [
            {
                "node_id": "draft",
                "agent_id": "draft-writer",
                "handoff_prompt": "Review this draft.",
            },
            {
                "node_id": "review",
                "agent_id": "reviewer",
                "handoff_prompt": "Rewrite this review.",
            },
            {"node_id": "final", "agent_id": "final-writer"},
        ],
        "loops": [
            {
                "id": "revision-loop",
                "mode": "fixed",
                "start_node_id": "draft",
                "end_node_id": "review",
                "iterations": 2,
            }
        ],
        "output_node_id": "final",
    }
    compiler = ExecutionPlanCompiler(
        project_store=_Store({"project": project}),
        graph_store=_Store({"writing": graph}),
        agent_store=agents,
        regex_collection_store=regexes,
    )
    runner = PiCoreNodeRunner(
        lambda node: {
            "base_url": provider.base_url,
            "api_key": "local-test-key",
            "api_format": "chat_completions",
            "model_id": node.model_id or "local-model",
        }
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )
    return runtime, runner


def _assert_success_trace(trace: dict, expected_output: str) -> None:
    assert trace["status"] == "succeeded"
    assert [node["node_id"] for node in trace["nodes"]] == [
        "draft.loop1",
        "review.loop1",
        "draft.loop2",
        "review.loop2",
        "final",
    ]
    assert [(node["source_node_id"], node["loop_id"], node["loop_iteration"]) for node in trace["nodes"]] == [
        ("draft", "revision-loop", 1), ("review", "revision-loop", 1),
        ("draft", "revision-loop", 2), ("review", "revision-loop", 2),
        ("final", None, None),
    ]
    assert all(node["state"] == "succeeded" for node in trace["nodes"])
    assert all(node["model_calls"][0]["usage"]["total_tokens"] == 18 for node in trace["nodes"])
    assert all(node["handoff_artifact"] is not None for node in trace["nodes"][:-1])
    assert "Rewrite this review." in trace["nodes"][-2]["handoff_artifact"]["content"]
    final = trace["nodes"][-1]
    assert final["final_output"] == expected_output
    transform = final["effective_config"]["regex_output_transform"]
    assert transform["raw"] == f"<content>{expected_output}</content>"
    assert transform["transformed"] == expected_output
    assert all(
        "regex_output_transform" not in node["effective_config"] for node in trace["nodes"][:-1]
    )


def test_two_player_turns_run_through_real_pi_plan_trace_retry_and_commit(tmp_path: Path):
    with _DeterministicSSEProvider() as provider:
        runtime, runner = _runtime(tmp_path, provider)

        first = runtime.submit("Ring the harbor bell.", "pi-turn-1")
        assert runner._process is None
        first_trace = runtime.graph_run_detail(runtime.graph_run_id_for_task(first.task_id))

        provider.fail_next = True
        failed = runtime.submit("Follow the lanterns home.", "pi-turn-2-failed")
        assert runner._process is None
        failed_run_id = runtime.graph_run_id_for_task(failed.task_id)
        failed_trace = runtime.graph_run_detail(failed_run_id)
        retry = runtime.retry_graph_run(failed_run_id, "pi-turn-2-retry")
        assert runner._process is None
        retry_trace = runtime.graph_run_detail(runtime.graph_run_id_for_task(retry.task_id))

    assert (first.status, first.revision) == ("succeeded", 1)
    assert first.commit_id
    assert failed.status == "failed_retryable"
    assert failed.commit_id is None
    assert failed.revision == 1
    assert failed_trace["status"] == "failed"
    assert failed_trace["nodes"][0]["error"]["code"] == "provider_unavailable"
    assert (retry.status, retry.revision) == ("succeeded", 2)
    assert retry.commit_id and retry.commit_id != first.commit_id
    assert retry_trace["retry_of"] == failed_run_id
    assert runtime.active_revision() == 2

    assert provider.success_count == 10
    assert len(provider.requests) == 11
    assert all(request["model"] == "local-model" for request in provider.requests)
    assert "Ring the harbor bell." in json.dumps(provider.requests[0])
    assert "Follow the lanterns home." in json.dumps(provider.requests[5])
    assert "The harbor bells answer." in json.dumps(provider.requests[6])

    _assert_success_trace(first_trace, "The harbor bells answer.")
    _assert_success_trace(retry_trace, "The lanterns guide the return.")

    projected = json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))
    assert [(turn["user"], turn["ai"]) for turn in projected] == [
        ("Ring the harbor bell.", "The harbor bells answer."),
        ("Follow the lanterns home.", "The lanterns guide the return."),
    ]
    event_types = [event.type for event in runtime.events_after(0)]
    assert event_types.count("turn.committed") == 2
    assert event_types.count("graph.run.finished") == 3
    assert event_types.count("graph.run.failed") >= 1
