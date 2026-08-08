from __future__ import annotations

import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.engine.provider import (  # noqa: E402
    AbortSignal,
    OpenAICompatibleProviderAdapter,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
)
from airp.engine.secret_store import LocalSecretStore  # noqa: E402
from airp.engine.provider_profiles import ProviderProfileService  # noqa: E402
from airp.engine.studio_library import ProviderProfileStore  # noqa: E402


class _Upstream:
    def __init__(self, *, responses_events=None):
        self.requests = []
        upstream = self
        self.responses_events = responses_events

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _record(self, body=None):
                upstream.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )

            def do_GET(self):  # noqa: N802
                self._record()
                payload = json.dumps(
                    {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length))
                self._record(body)
                if self.path.endswith("/chat/completions"):
                    events = [
                        {"choices": [{"delta": {"content": "Chat "}}]},
                        {"choices": [{"delta": {"content": "works"}, "finish_reason": "stop"}]},
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 4,
                                "completion_tokens": 2,
                                "total_tokens": 6,
                            },
                        },
                    ]
                else:
                    events = upstream.responses_events or [
                        {"type": "response.output_text.delta", "delta": "Responses "},
                        {"type": "response.output_text.delta", "delta": "works"},
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                            },
                        },
                    ]
                payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
                encoded = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _request():
    return ProviderRequest(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        model="deepseek-chat",
        metadata={"task_id": "task-safe"},
    )


def test_local_secret_store_replaces_deletes_and_redacts_without_exposing_values(tmp_path):
    secret = "sk-do-not-leak-123"
    store = LocalSecretStore(tmp_path / "studio" / "secrets.json")

    store.set("provider-a", secret)
    assert store.has("provider-a") is True
    assert store.get("provider-a") == secret
    assert store.redact({"message": f"Bearer {secret}", "nested": [secret]}) == {
        "message": "Bearer [REDACTED]",
        "nested": ["[REDACTED]"],
    }
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    store.set("provider-a", "replacement")
    assert LocalSecretStore(store.path).get("provider-a") == "replacement"
    assert secret not in store.path.read_text(encoding="utf-8")
    assert store.delete("provider-a") is True
    assert store.get("provider-a") is None
    assert store.delete("provider-a") is False


@pytest.mark.parametrize(
    ("api_format", "path", "text", "usage"),
    [
        ("chat_completions", "/v1/chat/completions", "Chat works", (4, 2, 6)),
        ("responses", "/v1/responses", "Responses works", (5, 3, 8)),
    ],
)
def test_openai_compatible_adapters_map_streams_to_common_contract(
    api_format, path, text, usage
):
    with _Upstream() as upstream:
        adapter = OpenAICompatibleProviderAdapter(
            base_url=upstream.base_url,
            api_key="sk-runtime-only",
            api_format=api_format,
        )
        items = list(adapter.stream(_request(), AbortSignal()))

    assert "".join(item.text or "" for item in items if isinstance(item, ProviderDelta)) == text
    result = next(item for item in items if isinstance(item, ProviderResult))
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == usage
    sent = upstream.requests[0]
    assert sent["path"] == path
    assert sent["authorization"] == "Bearer sk-runtime-only"
    assert sent["body"]["model"] == "deepseek-chat"
    assert sent["body"]["stream"] is True


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
def test_openai_compatible_adapters_translate_follow_up_tool_messages(api_format):
    request = ProviderRequest(
        messages=[
            {"role": "user", "content": "look it up"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "name": "lookup", "args": {"id": 7}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "lookup",
                "content": {"ok": True},
            },
        ],
        tools=[
            {
                "name": "lookup",
                "description": "Look up a record",
                "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}},
            }
        ],
        model="deepseek-chat",
        metadata={},
    )
    with _Upstream() as upstream:
        adapter = OpenAICompatibleProviderAdapter(
            base_url=upstream.base_url,
            api_key="secret",
            api_format=api_format,
        )
        list(adapter.stream(request, AbortSignal()))
        body = upstream.requests[0]["body"]

    if api_format == "chat_completions":
        assert body["messages"][1]["tool_calls"][0]["function"] == {
            "name": "lookup",
            "arguments": '{"id": 7}',
        }
        assert body["messages"][2]["content"] == '{"ok": true}'
        assert body["tools"][0]["function"]["name"] == "lookup"
    else:
        assert body["input"][2] == {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"id": 7}',
        }
        assert body["input"][3] == {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"ok": true}',
        }
        assert body["tools"][0]["name"] == "lookup"


@pytest.mark.parametrize("api_format", ["chat_completions", "responses"])
def test_openai_compatible_adapter_forwards_request_parameters(api_format):
    request = ProviderRequest(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        model="deepseek-chat",
        parameters={
            "temperature": 0.4,
            "max_output_tokens": 123,
            "response_format": {"type": "json_object"},
        },
        metadata={},
    )
    with _Upstream() as upstream:
        adapter = OpenAICompatibleProviderAdapter(
            base_url=upstream.base_url,
            api_key="secret",
            api_format=api_format,
        )
        list(adapter.stream(request, AbortSignal()))
        body = upstream.requests[0]["body"]

    assert body["temperature"] == 0.4
    assert body["max_output_tokens"] == 123
    assert body["response_format"] == {"type": "json_object"}


def test_adapter_discovers_models_and_classifies_redacted_provider_errors():
    with _Upstream() as upstream:
        adapter = OpenAICompatibleProviderAdapter(
            base_url=upstream.base_url,
            api_key="sk-runtime-only",
            api_format="chat_completions",
        )
        assert adapter.discover_models() == ["deepseek-chat", "deepseek-reasoner"]
        assert adapter.test_connection() == ["deepseek-chat", "deepseek-reasoner"]

    adapter = OpenAICompatibleProviderAdapter(
        base_url="http://127.0.0.1:1/v1",
        api_key="sk-must-not-appear",
        api_format="chat_completions",
        timeout=0.1,
    )
    with pytest.raises(ProviderError) as caught:
        adapter.discover_models()
    assert caught.value.category == "provider_unavailable"
    assert caught.value.retryable is True
    assert "sk-must-not-appear" not in str(caught.value)


def test_responses_failure_event_uses_stable_classification_and_redacts_secret():
    secret = "sk-echoed-by-provider"
    events = [
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": f"Rate limit for bearer {secret}",
                }
            },
        }
    ]
    with _Upstream(responses_events=events) as upstream:
        adapter = OpenAICompatibleProviderAdapter(
            base_url=upstream.base_url,
            api_key=secret,
            api_format="responses",
        )
        with pytest.raises(ProviderError) as caught:
            list(adapter.stream(_request(), AbortSignal()))

    assert caught.value.category == "provider_unavailable"
    assert caught.value.retryable is True
    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_provider_profile_execution_resolves_protocol_and_secret(tmp_path):
    styles = tmp_path / "styles"
    profiles = ProviderProfileStore(styles)
    profile = profiles.create_profile(
        {
            "id": "deepseek-profile",
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_format": "chat_completions",
            "model_ids": ["deepseek-chat"],
        }
    )
    secrets = LocalSecretStore(styles / "studio" / "secrets.json")
    secrets.set(profile["id"], "runtime-secret")
    adapter = ProviderProfileService(profiles, secrets).execution_adapter(
        profile["id"], "deepseek-chat"
    )

    assert isinstance(adapter, OpenAICompatibleProviderAdapter)
    assert adapter._api_format == "chat_completions"
    assert adapter._api_key == "runtime-secret"

    sidecar = ProviderProfileService(profiles, secrets).sidecar_execution_config(
        profile["id"], "deepseek-chat"
    )
    assert sidecar == {
        "base_url": "https://api.deepseek.com",
        "api_key": "runtime-secret",
        "api_format": "chat_completions",
        "model_id": "deepseek-chat",
    }


def test_provider_profile_execution_uses_request_timeout_not_discovery_timeout(tmp_path):
    styles = tmp_path / "styles"
    profiles = ProviderProfileStore(styles)
    profile = profiles.create_profile(
        {
            "id": "slow-provider",
            "name": "Slow Provider",
            "base_url": "https://example.test/v1",
            "api_format": "chat_completions",
            "model_ids": ["writer"],
        }
    )
    secrets = LocalSecretStore(styles / "studio" / "secrets.json")
    secrets.set(profile["id"], "runtime-secret")
    service = ProviderProfileService(
        profiles,
        secrets,
        discovery_timeout=3.0,
        request_timeout=77.0,
    )

    adapter = service.execution_adapter(profile["id"], "writer")

    assert adapter._timeout == 77.0
