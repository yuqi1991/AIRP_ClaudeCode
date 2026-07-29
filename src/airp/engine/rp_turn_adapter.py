"""The first-party RP Turn Adapter.

This adapter intentionally owns the existing tagged-text parser.  The parser
remains available through ``engine.turn_parser`` for legacy callers, while the
framework only sees the resulting opaque Artifact before this seam.
"""

from __future__ import annotations

from typing import Any

from airp.engine.graph_runtime import AgentArtifact
from airp.engine.turn_parser import parse_turn_text


class RPTurnAdapter:
    """Interpret a text Artifact using AIRP's current RP turn protocol."""

    adapter_id = "rp"

    def __init__(
        self,
        *,
        opening_instruction: str | None = None,
        required_final_node_role: str | None = None,
        tag_patterns: dict[str, str] | None = None,
    ) -> None:
        # ``None`` is the legacy compatibility mode. It supplies no writing
        # instruction; a Project-selected adapter can configure one explicitly.
        self._opening_instruction = opening_instruction
        self._required_final_node_role = required_final_node_role
        self._tag_patterns = dict(tag_patterns or {})

    @classmethod
    def from_project(cls, project: dict[str, Any] | None):
        """Build the selected first-party adapter from a Project definition."""
        spec = project.get("turn_adapter") if isinstance(project, dict) else None
        if not isinstance(spec, dict):
            return cls(opening_instruction="{{node_instruction}}", required_final_node_role=None)
        adapter_id = spec.get("id") or "rp"
        if adapter_id != cls.adapter_id:
            raise ValueError(f"unsupported turn adapter: {adapter_id}")
        config = spec.get("config") if isinstance(spec.get("config"), dict) else {}
        opening = config.get("opening_instruction", "{{node_instruction}}")
        role = config.get("required_final_node_role")
        patterns = config.get("tag_patterns") if isinstance(config.get("tag_patterns"), dict) else None
        return cls(
            opening_instruction=opening,
            required_final_node_role=role,
            tag_patterns=patterns,
        )

    def validate_opening_plan(self, config: dict[str, Any]) -> None:
        """Validate the legacy opening shape owned by the RP adapter.

        The framework does not assign semantic roles to graph nodes.  This
        compatibility rule remains here because the current RP adapter needs
        a final narrative director to produce a tagged opening turn.
        """
        graph = config.get("graph") if isinstance(config, dict) else None
        nodes = [node for node in (graph or {}).get("nodes", []) if node.get("enabled", True)]
        if self._required_final_node_role and (
            not nodes or nodes[-1].get("role") != self._required_final_node_role
        ):
            raise ValueError(
                "generated opening requires a final "
                f"{self._required_final_node_role} node"
            )

    def opening_instruction(self, node_instruction: str = "") -> str:
        """Return the RP-specific instruction used for generated openings."""
        instruction = self._opening_instruction or ""
        if "{{node_instruction}}" in instruction:
            return instruction.replace("{{node_instruction}}", node_instruction)
        if instruction and node_instruction:
            return instruction + "\n" + node_instruction
        return instruction or node_instruction

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
        return parse_turn_text(
            content,
            fallback_input=player_input,
            tag_patterns=self._tag_patterns,
        )
