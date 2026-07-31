from __future__ import annotations

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.agent_definitions import AgentDefinitionStore
from engine.context_compiler import ContextCompileRequest, ContextPolicy, expand_macros
from engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler
from engine.node_runner import ProviderNodeRunner
from engine.provider import FakeProvider


def test_new_agent_is_instruction_only_and_expands_nested_runtime_macros(tmp_path: Path):
    store = AgentDefinitionStore(tmp_path)
    created = store.create_agent(
        {
            "agent_id": "director",
            "name": "Narrative Director",
            "instruction": (
                "风格={{settings.style}}; 卡片={{card_facts.name}}; "
                "输入={{player_input}}; 回合={{recent_turns}}"
            ),
        }
    )

    assert "prompt_preset_id" not in created
    preview = store.preview_agent(
        "director",
        {
            "project_input": "玩家走进大厅",
            "context": {
                "settings": {"style": "冷峻"},
                "card_facts": {"name": "刻晴"},
                "recent_turns": [{"user": "你好", "assistant": "欢迎"}],
            },
        },
    )

    assert [item["kind"] for item in preview["provenance"]] == [
        "instruction",
        "project_input",
        "handoff",
        "tool_protocol",
    ]
    assert preview["messages"][0]["content"] == (
        '风格=冷峻; 卡片=刻晴; 输入=玩家走进大厅; '
        '回合=[{"assistant": "欢迎", "user": "你好"}]'
    )


def test_unknown_instruction_macro_is_preserved_for_debugging(tmp_path: Path):
    store = AgentDefinitionStore(tmp_path)
    store.create_agent(
        {
            "agent_id": "writer",
            "name": "Writer",
            "instruction": "keep {{future_macro}} and {{future.value}} visible",
        }
    )

    preview = store.preview_agent("writer", {"context": {}})

    assert preview["messages"][0]["content"] == "keep {{future_macro}} and {{future.value}} visible"
    assert preview["instruction_template"] == "keep {{future_macro}} and {{future.value}} visible"
    assert "settings" in preview["available_macros"]


def test_execution_plan_freezes_macro_expansion_from_runtime_context(tmp_path: Path):
    store = AgentDefinitionStore(tmp_path)
    store.create_agent(
        {
            "agent_id": "director",
            "name": "Director",
            "instruction": "state={{current_state.phase}}",
        }
    )
    plan = ExecutionPlanCompiler(agent_store=store).compile(
        project={"id": "project"},
        graph={
            "id": "story",
            "name": "Story",
            "nodes": [{"id": "director-node", "agent_id": "director"}],
        },
        player_input="继续",
        context={"current_state": {"phase": "夜晚"}},
    )

    assert plan.graph.nodes[0].agent.prompt[0]["content"] == "state=夜晚"


def test_studio_agent_editor_uses_instruction_and_context_without_prompt_preset():
    page = (SKILLS / "styles" / "studio.html").read_text(encoding="utf-8")

    assert 'id="agent-instruction"' in page
    assert 'id="preview-context"' in page
    assert 'id="agent-preset"' not in page
    assert "/v1/studio/prompt-presets" not in page


def test_node_runner_expands_handoff_macro_from_the_current_artifact(tmp_path: Path):
    store = AgentDefinitionStore(tmp_path)
    store.create_agent(
        {
            "agent_id": "reviewer",
            "name": "Reviewer",
            "instruction": "Review this handoff: {{handoff}}",
        }
    )
    plan = ExecutionPlanCompiler(agent_store=store).compile(
        project={"id": "project"},
        graph={"id": "story", "nodes": [{"id": "review", "agent_id": "reviewer"}]},
        player_input="start",
    )
    provider = FakeProvider([{"type": "text", "text": "ok"}, {"type": "final"}], model="reviewer")

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0],
        AgentArtifact.text("draft"),
    )

    assert result.ok
    assert provider.requests[0].messages[0]["content"] == "Review this handoff: draft"


def test_manifest_macros_do_not_preload_worldbook_reference_text():
    request = ContextCompileRequest(
        session_id="session",
        task_id="task",
        base_revision=0,
        player_input="继续",
        snapshot={
            "worldbook_catalog": [{"title": "境界", "usage": "境界词汇"}],
            "worldbook_reference": "PRIVATE FULL WORLD BOOK",
            "worldbook_user": "PRIVATE USER NOTES",
        },
        policy=ContextPolicy(version="test", token_budget=1000),
    )

    assert expand_macros("{{worldbook_reference}}", request) == "{{worldbook_reference}}"
    assert expand_macros("{{worldbook_catalog}}", request) == '[{"title": "境界", "usage": "境界词汇"}]'
