"""Provider-facing Node Runner implementations.

This module depends on provider adapters; ``engine.graph_runtime`` does not.
The runner turns provider output into the stable ``NodeResult`` / ``AgentArtifact``
boundary consumed by Graph Runtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable

from airp.engine.graph_runtime import AgentArtifact, GraphNodePlan, NodeResult
from airp.engine.macros import build_context, expand_template
from airp.engine.provider import (
    AbortSignal,
    ProviderAborted,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
)


class ProviderNodeRunner:
    """Run one resolved node through an injected Provider Adapter.

    ``provider_factory`` owns protocol/profile resolution.  ``tool_handler`` is
    optional and receives ``(name, args)``; without one, a tool request is a
    deterministic node failure rather than an implicit bypass of permissions.
    """

    def __init__(
        self,
        provider_factory: Callable[[GraphNodePlan], Any],
        *,
        tool_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        max_tool_rounds: int = 8,
        signal_factory: Callable[[], AbortSignal] = AbortSignal,
    ) -> None:
        if not callable(provider_factory):
            raise TypeError("ProviderNodeRunner requires a provider factory")
        self.provider_factory = provider_factory
        self.tool_handler = tool_handler
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.signal_factory = signal_factory

    def run(
        self,
        node: GraphNodePlan,
        input_artifact: AgentArtifact,
        *,
        observer: Any = None,
    ) -> NodeResult:
        try:
            provider = self.provider_factory(node)
            runtime_context = build_context(
                {
                    "handoff": input_artifact.content,
                    "node_input": input_artifact.content,
                    "input_artifact": input_artifact.to_dict(),
                }
            )
            messages = expand_template(
                [dict(message) for message in node.agent.prompt],
                runtime_context,
                preserve_unknown=True,
            )
            messages.append({"role": "user", "content": self._content(input_artifact.content)})
            tools = self._tools(node)
            model = node.model_id or node.agent.model_id or provider.model_id(node.agent.agent_id)
            parameters = self._parameters(node)
            signal = self.signal_factory()

            for call_ordinal in range(1, self.max_tool_rounds + 1):
                request = ProviderRequest(
                    messages=messages,
                    tools=tools,
                    model=model,
                    metadata={"node_id": node.node_id, "agent_id": node.agent_id},
                    parameters=parameters,
                )
                self._notify(observer, "model_call_started", node, call_ordinal, request)
                text, tool_calls, provider_result = self._stream(
                    provider,
                    request,
                    signal,
                    observer=observer,
                    node=node,
                    call_ordinal=call_ordinal,
                )
                self._notify(
                    observer,
                    "model_call_finished",
                    node,
                    call_ordinal,
                    request,
                    text,
                    provider_result,
                )
                if tool_calls:
                    if self.tool_handler is None:
                        return NodeResult.failed(
                            {"code": "tool_handler_unavailable", "calls": tool_calls}
                        )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": text,
                            "tool_calls": tool_calls,
                        }
                    )
                    for call in tool_calls:
                        self._notify(observer, "tool_call_started", node, call)
                        try:
                            value = self.tool_handler(call["name"], call.get("args") or {})
                        except Exception as exc:
                            self._notify(observer, "tool_call_finished", node, call, None, exc)
                            raise
                        self._notify(observer, "tool_call_finished", node, call, value, None)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": call["name"],
                                "content": self._content(value),
                            }
                        )
                    continue
                return NodeResult.succeeded(AgentArtifact.text(text))
            return NodeResult.failed("node tool-call round limit exceeded")
        except ProviderAborted as exc:
            return NodeResult.failed({"code": "aborted", "message": str(exc)})
        except ProviderError as exc:
            return NodeResult.failed(
                {
                    "code": exc.category,
                    "message": str(exc),
                    "retryable": bool(exc.retryable),
                }
            )
        except Exception as exc:
            return NodeResult.failed({"code": "node_runner_failed", "message": str(exc)})

    @staticmethod
    def _stream(provider, request, signal, *, observer=None, node=None, call_ordinal=0):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        provider_result = None
        for item in provider.stream(request, signal):
            if isinstance(item, ProviderDelta):
                if item.text:
                    text_parts.append(item.text)
                    ProviderNodeRunner._notify(observer, "node_delta", node, item.text)
                if item.tool_call:
                    tool_calls.append(
                        {
                            "id": item.tool_call.get("id"),
                            "name": item.tool_call.get("name"),
                            "args": item.tool_call.get("args") or {},
                        }
                    )
            elif isinstance(item, ProviderResult):
                provider_result = item
        return "".join(text_parts), tool_calls, provider_result

    @staticmethod
    def _notify(observer, method, *args):
        callback = getattr(observer, method, None) if observer is not None else None
        if callable(callback):
            try:
                callback(*args)
            except Exception:
                return

    @staticmethod
    def _tools(node: GraphNodePlan) -> list[dict[str, Any]]:
        tools = node.agent.effective_config.get("tools") if isinstance(node.agent.effective_config, dict) else None
        if isinstance(tools, list):
            return [dict(tool) for tool in tools if isinstance(tool, dict)]
        return [{"name": name} for name in node.agent.tool_allowlist]

    @staticmethod
    def _parameters(node: GraphNodePlan) -> dict[str, Any]:
        """Merge the resolved Agent controls with node-local overrides.

        ``GraphRuntime`` resolves Agent and node definitions before this seam,
        so the Agent carries the complete effective configuration. Keeping the
        merge here makes the provider request inspectable without coupling the
        provider module to Agent Definition storage.
        """
        parameters: dict[str, Any] = {}
        for source in (node.agent.generation, node.agent.advanced):
            if isinstance(source, Mapping):
                parameters.update(source)
        return parameters

    @staticmethod
    def _content(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
