from __future__ import annotations

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from airp.engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler, GraphRuntime, NodeExecutionContext
from airp.engine.pi_node_runner import PiCoreNodeRunner


class _PiUpstream:
    """Small OpenAI-compatible stream that makes Pi perform one tool round."""

    def __init__(self):
        self.requests: list[dict] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                return

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                upstream.requests.append(body)
                call_number = len(upstream.requests)
                if call_number == 1:
                    events = [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "lookup-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "get_recent_memory",
                                                    "arguments": "{}",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        }
                    ]
                elif call_number == 2:
                    events = [
                        {"choices": [{"delta": {"content": "draft body"}, "finish_reason": "stop"}]},
                        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
                    ]
                else:
                    events = [
                        {"choices": [{"delta": {"content": "final body"}, "finish_reason": "stop"}]},
                        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
                    ]
                payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
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


class _Observer:
    def __init__(self):
        self.started: list[tuple[str, int]] = []
        self.finished: list[tuple[str, int, str]] = []
        self.tools: list[tuple[str, str]] = []
        self.deltas: list[str] = []

    def model_call_started(self, node, ordinal, _request):
        self.started.append((node.node_id, ordinal))

    def model_call_finished(self, node, ordinal, _request, text, _result):
        self.finished.append((node.node_id, ordinal, text))

    def tool_call_started(self, node, call):
        self.tools.append((node.node_id, call["name"]))

    def node_delta(self, _node, text):
        self.deltas.append(text)


def _agent(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "name": "Writer",
        "instruction": "Use the supplied tools when useful.",
        "provider_profile_id": "provider",
        "model_id": "pi-test-model",
        "tool_allowlist": ["get_recent_memory"],
        "prompt": [{"role": "system", "content": "You are a careful writing agent."}],
    }


def test_pi_core_runner_continues_tool_loop_preserves_agent_memory_and_closes_execution():
    graph = {
        "id": "pi-handoff",
        "name": "Pi Handoff",
        "mode": "handoff",
        "nodes": [
            {"node_id": "draft", "agent_id": "writer", "handoff_prompt": "Review this draft:"},
            {
                "node_id": "final",
                "agent_id": "writer",
                "model_id": "pi-override-model",
                "generation": {"temperature": 0.7},
            },
        ],
        "output_node_id": "final",
    }
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph=graph,
        agents={"writer": _agent("writer")},
        player_input="Write a scene.",
    )
    tool_calls: list[tuple[str, dict]] = []
    observer = _Observer()
    with _PiUpstream() as upstream:
        runner = PiCoreNodeRunner(
            lambda node: {
                "base_url": upstream.base_url,
                "api_key": "pi-test-secret",
                "api_format": "chat_completions",
                "model_id": node.model_id or "pi-test-model",
            },
            tool_handler=lambda name, args: tool_calls.append((name, args)) or {"memory": "loaded"},
        )
        try:
            result = GraphRuntime(runner).run(
                plan,
                observer=observer,
                execution_context=NodeExecutionContext(execution_id="pi-task"),
            )
            assert runner._process is None
        finally:
            runner.shutdown()

    assert result.ok is True
    assert result.output_artifact is not None
    assert result.output_artifact.content == "final body"
    assert tool_calls == [("get_recent_memory", {})]
    assert observer.started == [("draft", 1), ("draft", 2), ("final", 1)]
    assert observer.finished == [("draft", 1, ""), ("draft", 2, "draft body"), ("final", 1, "final body")]
    assert observer.tools == [("draft", "get_recent_memory")]
    assert "draft body" in "".join(observer.deltas)
    assert len(upstream.requests) == 3
    # The repeated writer keeps only its task-local Pi transcript, including
    # the prior draft, before AIRP injects the Handoff input.
    second_node_messages = upstream.requests[2]["messages"]
    assert upstream.requests[2]["model"] == "pi-override-model"
    assert upstream.requests[2]["temperature"] == 0.7
    assert any(message.get("content") == "draft body" for message in second_node_messages)
    assert any(
        "Review this draft:\n\ndraft body" in str(block.get("text"))
        for message in second_node_messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict)
    )
    assert "pi-test-secret" not in json.dumps(upstream.requests)


class _CloseFailureProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.terminated = False
        self.waited = False

    def poll(self):
        return 0 if self.waited else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.waited = True


def test_pi_close_execution_reaps_process_and_closes_pipes_when_close_write_fails(monkeypatch):
    runner = PiCoreNodeRunner(lambda _node: {})
    process = _CloseFailureProcess()
    runner._process = process
    monkeypatch.setattr(runner, "_send", lambda _message: (_ for _ in ()).throw(BrokenPipeError("closed")))

    runner.close_execution("execution")

    assert runner._process is None
    assert process.terminated is True
    assert process.waited is True
    assert process.stdin.closed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_pi_sidecar_stderr_is_bounded_and_secret_redacted_in_failure(tmp_path):
    sidecar = tmp_path / "failing_sidecar.py"
    sidecar.write_text(
        "import sys\n"
        "sys.stderr.write('discard-me-' + ('x' * 20000) + '-api-secret-tail')\n"
        "sys.stderr.flush()\n",
        encoding="utf-8",
    )
    graph = {
        "id": "pi-error",
        "nodes": [{"node_id": "only", "agent_id": "writer"}],
        "output_node_id": "only",
    }
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph=graph,
        agents={"writer": _agent("writer")},
        player_input="Write.",
    )
    runner = PiCoreNodeRunner(
        lambda _node: {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "api-secret",
            "api_format": "chat_completions",
            "model_id": "pi-test-model",
        },
        node_binary=sys.executable,
        sidecar_path=sidecar,
    )

    result = GraphRuntime(runner).run(
        plan,
        execution_context=NodeExecutionContext(execution_id="stderr-task"),
    )

    assert result.error["code"] == "pi_sidecar_failed"
    message = result.error["message"]
    assert "[REDACTED]-tail" in message
    assert "api-secret" not in message
    assert "discard-me" not in message
    assert len(message) <= 17000


def test_pi_reconfigures_system_and_tools_without_losing_private_transcript():
    first_agent = _agent("writer")
    second_agent = {
        **_agent("writer"),
        "prompt": [{"role": "system", "content": "Use the new system configuration."}],
        "tool_allowlist": [],
        "generation": {"temperature": 0.8},
        "model_id": "pi-reconfigured-model",
    }
    first_plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "first",
            "nodes": [{"node_id": "first", "agent_id": "writer"}],
            "output_node_id": "first",
        },
        agents={"writer": first_agent},
        player_input="Draft.",
    )
    second_plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "second",
            "nodes": [{"node_id": "second", "agent_id": "writer"}],
            "output_node_id": "second",
        },
        agents={"writer": second_agent},
        player_input="Revise.",
    )

    with _PiUpstream() as upstream:
        runner = PiCoreNodeRunner(
            lambda node: {
                "base_url": upstream.base_url,
                "api_key": "pi-test-secret",
                "api_format": "chat_completions",
                "model_id": node.model_id or node.agent.model_id or "pi-test-model",
            },
            tool_handler=lambda _name, _args: {"memory": "loaded"},
        )
        context = NodeExecutionContext(execution_id="reconfigure-task")
        try:
            first = runner.run(first_plan.graph.nodes[0], AgentArtifact.input("Draft."), execution_context=context)
            second = runner.run(second_plan.graph.nodes[0], AgentArtifact.input("Revise."), execution_context=context)
        finally:
            runner.close_execution("reconfigure-task")

    assert first.ok is True
    assert second.ok is True
    request = upstream.requests[2]
    assert request["model"] == "pi-reconfigured-model"
    assert request["temperature"] == 0.8
    assert request.get("tools") in (None, [])
    assert any(message.get("content") == "draft body" for message in request["messages"])
    assert any(message.get("content") == "Use the new system configuration." for message in request["messages"])
