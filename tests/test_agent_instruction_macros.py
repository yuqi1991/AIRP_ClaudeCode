from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.engine.agent_definitions import AgentDefinitionStore
from airp.engine.context_compiler import ContextCompileRequest, ContextPolicy, expand_macros
from airp.engine.macros import DEFAULT_MACRO_ROOTS
from airp.engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler
from airp.engine.node_runner import ProviderNodeRunner
from airp.engine.provider import FakeProvider
from airp.host.rp.session_runtime import SessionTurnRuntime


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


def test_task_snapshot_flattens_imported_card_envelope_for_nested_macros(tmp_path: Path):
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(
        '{"spec":"chara_card_v2","data":{"name":"刻晴","scenario":"璃月港的夜雨"}}',
        encoding="utf-8",
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=tmp_path / "styles",
        bootstrap_legacy_history=False,
    )

    snapshot = runtime._source_snapshot(0, player_input="继续")

    assert snapshot["card_facts"]["name"] == "刻晴"
    assert snapshot["card_facts"]["scenario"] == "璃月港的夜雨"
    assert "data" not in snapshot["card_facts"]


def test_studio_agent_editor_uses_instruction_and_context_without_prompt_preset():
    page = (REPO_ROOT / "src" / "airp" / "web" / "studio.html").read_text(encoding="utf-8")

    assert 'id="agent-instruction"' in page
    assert 'id="preview-context"' in page
    assert 'id="agent-preset"' not in page
    assert "/v1/studio/prompt-presets" not in page


def test_integrated_agent_editor_documents_every_supported_macro_root():
    page = (REPO_ROOT / "src" / "airp" / "web" / "index.html").read_text(encoding="utf-8")

    documented = set(re.findall(r'data-agent-macro="([A-Za-z_][A-Za-z0-9_.-]*)"', page))
    assert documented == set(DEFAULT_MACRO_ROOTS)
    assert "{{root.path}}" in page
    assert "{{card_facts.description}}" in page
    assert "{{card_facts.personality}}" in page
    assert "{{card_facts.scenario}}" in page
    assert "{{card_facts.first_mes}}" in page
    assert "不存在的宏或路径会保留原占位符" in page
    assert "不会隐式暴露完整世界书正文" in page
    assert "提示词预览" in page


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
