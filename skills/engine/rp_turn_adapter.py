"""The first-party RP Turn Adapter.

This adapter intentionally owns the existing tagged-text parser.  The parser
remains available through ``engine.turn_parser`` for legacy callers, while the
framework only sees the resulting opaque Artifact before this seam.
"""

from __future__ import annotations

from typing import Any

from engine.graph_runtime import AgentArtifact
from engine.turn_parser import parse_turn_text


class RPTurnAdapter:
    """Interpret a text Artifact using AIRP's current RP turn protocol."""

    adapter_id = "rp"

    def validate_opening_plan(self, config: dict[str, Any]) -> None:
        """Validate the legacy opening shape owned by the RP adapter.

        The framework does not assign semantic roles to graph nodes.  This
        compatibility rule remains here because the current RP adapter needs
        a final narrative director to produce a tagged opening turn.
        """
        graph = config.get("graph") if isinstance(config, dict) else None
        nodes = [node for node in (graph or {}).get("nodes", []) if node.get("enabled", True)]
        if not nodes or nodes[-1].get("role") != "narrative_director":
            raise ValueError("generated opening requires a final narrative_director node")

    def opening_instruction(self, node_instruction: str = "") -> str:
        """Return the RP-specific instruction used for generated openings."""
        instruction = (
            "请根据角色设定生成一段自然的中文开场叙事。不要虚构玩家已经做出的行动。"
            "输出 <content>、<summary> 和 <options>；如需初始化变量，可输出 <UpdateVariable>。"
        )
        if node_instruction:
            instruction += "\n本次写作节点指令：" + node_instruction
        return instruction

    def interpret(
        self,
        artifact: AgentArtifact,
        *,
        player_input: str = "",
        context: Any = None,
    ) -> Any:
        del context
        content = artifact.content
        if not isinstance(content, str):
            raise TypeError("RP Turn Adapter expects a text Artifact")
        return parse_turn_text(content, fallback_input=player_input)
