from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.context_compiler import ContextPolicy  # noqa: E402
from engine.quality import DefaultQualityGate, QualityContext  # noqa: E402
from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime  # noqa: E402
from engine.runtime_config import RuntimeConfigError, RuntimeConfigStore  # noqa: E402


def _write_config(styles: Path, *, policy="Policy {{charName}}", model="deepseek-test"):
    (styles / "presets").mkdir(parents=True, exist_ok=True)
    (styles / "graphs").mkdir(exist_ok=True)
    (styles / "prompt_fragments").mkdir(exist_ok=True)
    (styles / "settings.json").write_text(
        json.dumps(
            {
                "charName": "格蕾丝",
                "style": "自定义",
                "wordCount": 9000,
                "runtime": {"preset_id": "custom", "graph_id": "main"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (styles / "prompt_fragments" / "policy.md").write_text(policy, encoding="utf-8")
    (styles / "presets" / "custom.json").write_text(
        json.dumps(
            {
                "id": "custom",
                "version": "7",
                "token_budget": 4000,
                "entries": [
                    {
                        "id": "policy",
                        "kind": "narrative_policy",
                        "role": "system",
                        "enabled": True,
                        "placement": "relative",
                        "depth": 0,
                        "order": 10,
                        "source": {"type": "markdown", "path": "prompt_fragments/policy.md"},
                    },
                    {
                        "id": "state",
                        "kind": "current_state",
                        "role": "user",
                        "enabled": True,
                        "placement": "relative",
                        "depth": 0,
                        "order": 20,
                        "content": "{{current_state}}",
                    },
                    {
                        "id": "input",
                        "kind": "player_input",
                        "role": "user",
                        "enabled": True,
                        "placement": "in_chat",
                        "depth": 0,
                        "order": 30,
                        "content": "{{player_input}}",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (styles / "graphs" / "main.json").write_text(
        json.dumps(
            {
                "id": "main",
                "version": "2",
                "mode": "sequential",
                "commit_validation_retries": 2,
                "nodes": [
                    {
                        "id": "director",
                        "role": "narrative_director",
                        "order": 0,
                        "provider": "deepseek",
                        "model": model,
                        "max_tool_rounds": 4,
                        "max_retries": 1,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_card(card: Path):
    card.mkdir()
    (card / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False), encoding="utf-8"
    )
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(
        json.dumps({"name": "格蕾丝"}, ensure_ascii=False), encoding="utf-8"
    )


def test_store_loads_ordered_entries_markdown_and_restricted_graph(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    _write_config(styles)

    frozen = RuntimeConfigStore(styles).freeze().data

    assert frozen["preset_id"] == "custom"
    assert [entry["id"] for entry in frozen["preset"]["entries"]] == ["policy", "state", "input"]
    assert frozen["preset"]["entries"][0]["raw_content"] == "Policy {{charName}}"
    assert frozen["preset"]["entries"][0]["source"]["type"] == "markdown"
    assert frozen["graph"]["mode"] == "sequential"
    assert frozen["graph"]["nodes"][0]["model"] == "deepseek-test"


def test_runtime_freezes_selected_files_per_submit_and_records_provenance(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    _write_config(styles)
    card = tmp_path / "card"
    _write_card(card)
    store = RuntimeConfigStore(styles)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        executor=FakeNarrativeExecutor(content="<p>短。</p>"),
        manifest_policy=ContextPolicy(version="runtime", token_budget=4000),
        runtime_config_store=store,
    )

    first = runtime.submit("第一轮", "first")
    first_manifest = runtime.manifest_for_task(first.task_id, 0)
    assert first.status == "succeeded"
    assert first_manifest["preset_id"] == "custom"
    assert first_manifest["runtime_config"]["graph_id"] == "main"
    policy = first_manifest["sections"][0]
    assert policy["content"] == "Policy 格蕾丝"
    assert policy["prompt_entry"]["id"] == "policy"
    assert policy["source"]["raw_hash"]
    assert policy["source"]["expanded_hash"]
    assert policy["source"]["placeholders"] == ["charName"]

    (styles / "prompt_fragments" / "policy.md").write_text("Changed {{charName}}", encoding="utf-8")
    duplicate = runtime.submit("不会替换", "first")
    assert duplicate.task_id == first.task_id
    assert runtime.manifest_for_task(first.task_id, 0)["sections"][0]["content"] == "Policy 格蕾丝"

    second = runtime.submit("第二轮", "second")
    second_manifest = runtime.manifest_for_task(second.task_id, 0)
    assert second_manifest["sections"][0]["content"] == "Changed 格蕾丝"
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        snapshots = [json.loads(row[0]) for row in connection.execute("SELECT source_snapshot FROM tasks ORDER BY rowid")]
    assert snapshots[0]["runtime_config"]["preset"]["entries"][0]["raw_content"] == "Policy {{charName}}"
    assert snapshots[1]["runtime_config"]["preset"]["entries"][0]["raw_content"] == "Changed {{charName}}"


def test_word_count_does_not_block_commit_but_empty_visible_content_does():
    gate = DefaultQualityGate()
    short = type("Draft", (), {"content": "<p>短</p>"})()
    empty = type("Draft", (), {"content": "<p>  </p>"})()

    assert gate.validate(short, QualityContext(settings={"wordCount": 9000})).ok is True
    verdict = gate.validate(empty, QualityContext(settings={"wordCount": 1} ))
    assert verdict.ok is False
    assert verdict.reasons == ("content_too_short",)


def test_invalid_placeholder_and_arbitrary_graph_provider_are_rejected(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    _write_config(styles)
    preset_path = styles / "presets" / "custom.json"
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    preset["entries"][0]["content"] = "{{ambient_time}}"
    preset["entries"][0].pop("source")
    preset_path.write_text(json.dumps(preset), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="unknown placeholder"):
        RuntimeConfigStore(styles).freeze()

    _write_config(styles)
    graph_path = styles / "graphs" / "main.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["provider"] = "arbitrary-shell-provider"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="unsupported graph provider"):
        RuntimeConfigStore(styles).freeze()
