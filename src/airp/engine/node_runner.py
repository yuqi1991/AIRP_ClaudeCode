"""Provider-facing Node Runner implementations.

This module depends on provider adapters; ``engine.graph_runtime`` does not.
The runner turns provider output into the stable ``NodeResult`` / ``AgentArtifact``
boundary consumed by Graph Runtime.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Callable

from airp.engine.graph_runtime import AgentArtifact, GraphNodePlan, NodeExecutionContext, NodeResult
from airp.engine.macros import build_context, expand_template
from airp.engine.provider import (
    AbortSignal,
    ProviderAborted,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
)
from airp.engine.regex_transformer import RegexTransformError, RegexTransformer


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
        regex_transformer: RegexTransformer | Callable[[GraphNodePlan], Any] | Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        if not callable(provider_factory):
            raise TypeError("ProviderNodeRunner requires a provider factory")
        self.provider_factory = provider_factory
        self.tool_handler = tool_handler
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.signal_factory = signal_factory
        self.regex_transformer = regex_transformer

    def run(
        self,
        node: GraphNodePlan,
        input_artifact: AgentArtifact,
        *,
        observer: Any = None,
        execution_context: NodeExecutionContext | None = None,
    ) -> NodeResult:
        active_request = None
        active_call_ordinal = None
        try:
            provider = self.provider_factory(node)
            regex_transformer = self._resolve_regex_transformer(node)
            input_content = input_artifact.content
            if regex_transformer is not None:
                input_result = regex_transformer.transform(input_content, target="input")
                self._notify(
                    observer,
                    "input_transformed",
                    node,
                    input_content,
                    input_result,
                )
                input_content = input_result.text
            context_artifact = input_artifact
            if input_content != input_artifact.content and isinstance(input_content, str):
                context_artifact = AgentArtifact.text(
                    input_content,
                    kind=input_artifact.kind,
                    content_type=input_artifact.content_type,
                    metadata=input_artifact.metadata,
                )
            runtime_context = build_context(
                {
                    "handoff": input_content,
                    "node_input": input_content,
                    "input_artifact": context_artifact.to_dict(),
                    "skills": (
                        execution_context.skill_catalog
                        if execution_context is not None and execution_context.skill_catalog is not None
                        else []
                    ),
                }
            )
            messages = expand_template(
                [dict(message) for message in node.agent.prompt],
                runtime_context,
                preserve_unknown=True,
            )
            messages.append({"role": "user", "content": self._content(input_content)})
            tools = self._tools(node, execution_context)
            model = node.model_id or node.agent.model_id or provider.model_id(node.agent.agent_id)
            parameters = self._parameters(node)
            signal = (
                execution_context.abort_signal
                if execution_context is not None and execution_context.abort_signal is not None
                else self.signal_factory()
            )

            for call_ordinal in range(1, self.max_tool_rounds + 1):
                request = ProviderRequest(
                    messages=messages,
                    tools=tools,
                    model=model,
                    metadata={"node_id": node.node_id, "agent_id": node.agent_id},
                    parameters=parameters,
                )
                active_request = request
                active_call_ordinal = call_ordinal
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
                active_request = None
                active_call_ordinal = None
                if tool_calls:
                    tool_handler = self._tool_handler_for(node, execution_context)
                    if tool_handler is None:
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
                            value = tool_handler(node, call["name"], call.get("args") or {})
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
                output_content = text
                if regex_transformer is not None:
                    output_result = regex_transformer.transform(text, target="output")
                    self._notify(
                        observer,
                        "output_transformed",
                        node,
                        text,
                        output_result,
                    )
                    output_content = output_result.text
                return NodeResult.succeeded(AgentArtifact.text(output_content))
            return NodeResult.failed("node tool-call round limit exceeded")
        except RegexTransformError as exc:
            self._notify(observer, "regex_transform_failed", node, exc)
            return NodeResult.failed(exc.to_dict())
        except ProviderAborted as exc:
            self._notify_model_call_failed(observer, node, active_call_ordinal, active_request, exc)
            return NodeResult.failed({"code": "aborted", "message": str(exc)})
        except ProviderError as exc:
            self._notify_model_call_failed(observer, node, active_call_ordinal, active_request, exc)
            return NodeResult.failed(
                {
                    "code": exc.category,
                    "message": str(exc),
                    "retryable": bool(exc.retryable),
                }
            )
        except Exception as exc:
            self._notify_model_call_failed(observer, node, active_call_ordinal, active_request, exc)
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

    @classmethod
    def _notify_model_call_failed(cls, observer, node, call_ordinal, request, error):
        """Close an in-flight model-call trace when the provider raises.

        A provider failure must remain visible in the same durable trace as a
        successful call; otherwise a node appears to have a permanently
        ``running`` model call after a timeout or cancellation.
        """
        if call_ordinal is None or request is None:
            return
        cls._notify(observer, "model_call_failed", node, call_ordinal, request, error)

    @staticmethod
    def _tools(
        node: GraphNodePlan,
        execution_context: NodeExecutionContext | None = None,
    ) -> list[dict[str, Any]]:
        registry = execution_context.tool_registry if execution_context is not None else None
        if registry is not None and callable(getattr(registry, "schemas", None)):
            return [dict(tool) for tool in registry.schemas(node.tool_allowlist)]
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

    def _resolve_regex_transformer(self, node: GraphNodePlan) -> RegexTransformer | None:
        """Resolve an optional frozen Agent collection without owning persistence.

        The explicit constructor seam is useful to runtimes that resolve the
        collection outside Graph Runtime.  The effective-config fallback keeps
        this runner compatible with plans that already embed a collection while
        leaving Agent/Project stores unaware of regex execution.
        """

        candidate: Any = self.regex_transformer
        if candidate is not None:
            if isinstance(candidate, RegexTransformer):
                return candidate
            if callable(candidate):
                candidate = candidate(node)
            if isinstance(candidate, RegexTransformer):
                return candidate
            if isinstance(candidate, Mapping):
                candidate = candidate.get("rules")
            if isinstance(candidate, (list, tuple)):
                return RegexTransformer(candidate)
            if candidate is None:
                return None
            raise TypeError("regex_transformer must be a RegexTransformer, rules, or factory")

        effective = getattr(node.agent, "effective_config", {})
        if isinstance(effective, Mapping):
            candidate = effective.get("regex_collection", effective.get("regex_rules"))
        if candidate is None:
            candidate = getattr(node.agent, "regex_collection", None)
        if isinstance(candidate, Mapping):
            candidate = candidate.get("rules")
        if isinstance(candidate, (list, tuple)):
            return RegexTransformer(candidate)
        return None

    def _tool_handler_for(
        self,
        node: GraphNodePlan,
        execution_context: NodeExecutionContext | None,
    ):
        if execution_context is not None and execution_context.tool_handler is not None:
            return execution_context.tool_handler
        registry = execution_context.tool_registry if execution_context is not None else None
        if registry is not None and callable(getattr(registry, "dispatch", None)):
            return lambda current_node, name, args: registry.dispatch(
                name,
                args,
                allowed=current_node.tool_allowlist,
            )
        if self.tool_handler is None:
            return None
        return lambda _node, name, args: self.tool_handler(name, args)

    @staticmethod
    def _content(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
