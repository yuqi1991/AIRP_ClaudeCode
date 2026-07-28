from __future__ import annotations

from engine.agent_graph import SequentialAgentGraph, SequentialGraphNode
from engine.director import NarrativeDirector, ProviderDrivenDirector
from engine.provider import RealProviderAdapter, runtime_provider_api_key


class DeterministicGraphDirector(NarrativeDirector):
    """Graph-aware mock node used by the real runtime startup path."""

    def __init__(self, node_id: str, role: str):
        self.node_id = node_id
        self.role = role

    def direct(self, handle, compiled) -> None:
        if self.role != "narrative_director":
            handle.set_final_text(f"{self.node_id}:{self.role}")
            return
        text = handle.task_text.strip() or "玩家行动"
        handle.set_final_text(
            "\n".join(
                [
                    f"<polished_input>{text}</polished_input>",
                    f"<content><p>Mock：{text}</p></content>",
                    f"<summary>Mock：{text}</summary>",
                    '<options><font color="#5a7a5a">继续行动</font></options>',
                ]
            )
        )


class RuntimeExecutorFactory:
    """Build one executor from a task's frozen sequential graph."""

    def __init__(self, *, mock: bool, base_url="https://api.deepseek.com"):
        self.mock = bool(mock)
        self.base_url = base_url

    def __call__(self, runtime_config):
        if not isinstance(runtime_config, dict):
            raise ValueError("task has no frozen runtime config")
        graph = runtime_config.get("graph") or {}
        nodes = [node for node in graph.get("nodes", []) if node.get("enabled", True)]
        if not nodes:
            raise ValueError("frozen runtime graph has no enabled nodes")
        graph_nodes = [self._build_node(node, runtime_config) for node in nodes]
        return SequentialAgentGraph(graph_nodes)

    def _build_node(self, node, runtime_config):
        if self.mock:
            director = DeterministicGraphDirector(node["id"], node["role"])
        else:
            provider_settings = (runtime_config.get("settings") or {}).get("provider") or {}
            adapter = RealProviderAdapter(
                mock=False,
                model=node["model"],
                base_url=provider_settings.get("base_url") or self.base_url,
                provider=node["provider"],
                runtime_api_key=runtime_provider_api_key(node["provider"]),
            )
            director = ProviderDrivenDirector(
                adapter,
                max_tool_rounds=node["max_tool_rounds"],
                max_retries=node["max_retries"],
                role=node["role"],
                model=node["model"],
                instruction=node.get("instruction", ""),
            )
        return SequentialGraphNode(node["id"], node["role"], director)
