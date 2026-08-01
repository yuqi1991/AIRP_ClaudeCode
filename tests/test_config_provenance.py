from __future__ import annotations

from airp.engine.graph_runtime import ExecutionPlan, ExecutionPlanCompiler


def test_execution_plan_freezes_configuration_revisions_and_hashes():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "game", "name": "Game", "revision": 3},
        graph={
            "id": "linear",
            "name": "Linear",
            "revision": 4,
            "nodes": [{"node_id": "writer-node", "agent_id": "writer", "order": 0}],
        },
        agents=[
            {
                "agent_id": "writer",
                "name": "Writer",
                "revision": 2,
                "instruction": "Write the scene.",
                "generation": {},
                "advanced": {},
                "tool_allowlist": [],
            }
        ],
        worldbooks=[{"id": "lore", "name": "Lore", "revision": 5, "entries": []}],
        player_input="Begin",
    )

    provenance = plan.provenance
    assert provenance["project"]["revision"] == 3
    assert provenance["graph"]["revision"] == 4
    assert provenance["agents"][0]["revision"] == 2
    assert provenance["worldbooks"][0]["revision"] == 5
    assert all(item["content_hash"] for item in provenance["agents"] + provenance["worldbooks"])
    round_trip = ExecutionPlan.from_dict(plan.to_dict())
    assert round_trip.provenance == provenance
