import json
import sqlite3
import sys
import threading
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.context_compiler import ContextCompileRequest, ContextPolicy, compile_context, replay_payload
from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime
from engine.worldbook import load_worldbook_entry


def context_snapshot():
    return {
        "card_facts": {"name": "测试角色", "description": "来自角色卡的设定"},
        "settings": {"style": "北棱特调", "wordCount": 600},
        "worldbook_catalog": [
            {"title": "海港", "section": "## 海港", "usage": "海港风俗；抵达码头时读取。"}
        ],
        "card_structure": {"has_events": True},
        "initvar": {"世界": {"时间": "1月1日 09:00"}},
        "current_state": {"世界": {"时间": "1月1日 10:00"}},
        "recent_memory": "玩家刚抵达海港。",
        "recent_turns": [{"revision": 1, "user": "我下船", "assistant": "雾从水面升起。"}],
    }


def test_compiler_builds_deterministic_catalog_only_payload():
    request = ContextCompileRequest(
        session_id="session-1",
        task_id="task-1",
        base_revision=1,
        player_input="我走向港口",
        snapshot=context_snapshot(),
        policy=ContextPolicy(version="test-v1", token_budget=4000),
    )

    first = compile_context(request)
    second = compile_context(request)

    assert first.payload == second.payload
    assert first.payload_hash == second.payload_hash
    assert first.stable_payload_hash == second.stable_payload_hash
    assert [section["kind"] for section in first.manifest["sections"]] == [
        "narrative_policy",
        "card_facts",
        "settings",
        "worldbook_catalog",
        "card_structure",
        "variable_baseline",
        "current_state",
        "recent_memory",
        "recent_turns",
        "player_input",
    ]
    assert [section["source"]["id"] for section in first.manifest["sections"]] == [
        "policy",
        "card_facts",
        "settings",
        "worldbook_catalog",
        "card_structure",
        "variable_baseline",
        "current_state",
        "recent_memory",
        "recent_turns",
        "player_input",
    ]
    assert all(section["source"]["hash"] for section in first.manifest["sections"])
    payload_text = json.dumps(first.payload, ensure_ascii=False)
    assert "海港风俗" in payload_text
    assert "海港正文不应自动进入" not in payload_text
    assert "任意旧文件" not in payload_text


def test_exact_title_loader_reads_only_matching_markdown_section(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / ".worldbook_index.json").write_text(
        json.dumps(
            [
                {"title": "海港", "section": "## 海港", "usage": "海港设定"},
                {"title": "王宫", "section": "## 王宫", "usage": "王宫设定"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (memory / "reference.md").write_text(
        "## 海港\n海港正文不应自动进入。\n### 子标题\n仍属于海港。\n## 王宫\n王宫正文。\n",
        encoding="utf-8",
    )

    entry = load_worldbook_entry(tmp_path, "海港")

    assert entry.title == "海港"
    assert "海港正文不应自动进入" in entry.content
    assert "仍属于海港" in entry.content
    assert "王宫正文" not in entry.content


def test_runtime_persists_manifest_before_executor_and_replays_payload(tmp_path):
    card_folder = tmp_path / "card"
    memory = card_folder / "memory"
    memory.mkdir(parents=True)
    (card_folder / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False), encoding="utf-8"
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    (memory / ".worldbook_index.json").write_text(
        json.dumps([{"title": "海港", "section": "## 海港", "usage": "海港设定"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "reference.md").write_text("## 海港\n海港正文不应自动进入。", encoding="utf-8")

    class InspectingExecutor(FakeNarrativeExecutor):
        def __init__(self, runtime_holder):
            super().__init__(content="<p>回合。</p>")
            self.runtime_holder = runtime_holder
            self.manifest = None

        def run(self, text, compiled_context):
            self.manifest = self.runtime_holder["runtime"].manifest_for_task(compiled_context.manifest["task_id"], 0)
            assert self.manifest is not None
            assert self.manifest["base_revision"] == 0
            assert self.manifest["payload"] == compiled_context.payload
            return super().run(text)

    holder = {}
    executor = InspectingExecutor(holder)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=executor,
        manifest_policy=ContextPolicy(version="runtime-v1", token_budget=4000),
    )
    holder["runtime"] = runtime
    result = runtime.submit(text="我来到海港", idempotency_key="submit-1")

    manifest = runtime.manifest_for_task(result.task_id, 0)
    assert manifest["base_revision"] == 0
    assert runtime.replay_manifest(manifest["id"]) == manifest["payload"]
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute("UPDATE context_manifests SET payload_json = ? WHERE id = ?", ("[]", manifest["id"]))
    try:
        runtime.replay_manifest(manifest["id"])
    except RuntimeError as error:
        assert str(error) == "persisted manifest payload hash mismatch"
    else:
        raise AssertionError("payload tampering must be detected")
    assert "海港正文不应自动进入" not in json.dumps(manifest["payload"], ensure_ascii=False)
    duplicate = runtime.submit(text="我来到海港", idempotency_key="submit-1")
    assert duplicate.task_id == result.task_id
    assert len(runtime.manifests_for_task(result.task_id)) == 1


def test_loaded_worldbook_appears_as_next_manifest_provenance_not_body(tmp_path):
    card_folder = tmp_path / "card"
    memory = card_folder / "memory"
    memory.mkdir(parents=True)
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    (memory / ".worldbook_index.json").write_text(
        json.dumps([{"title": "海港", "section": "## 海港", "usage": "海港设定"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "reference.md").write_text("## 海港\n只该由工具读取的正文。", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
    )
    result = runtime.submit(text="我来到海港", idempotency_key="submit-1")
    first = runtime.manifest_for_task(result.task_id, 0)
    assert "只该由工具读取的正文" not in json.dumps(first["payload"], ensure_ascii=False)

    runtime.load_worldbook_for_task(result.task_id, "海港", "agent requested exact title")
    second = runtime.compile_follow_up_manifest(result.task_id, "继续描述海港")
    second_payload = json.dumps(second.payload, ensure_ascii=False)

    entry_section = next(section for section in second.manifest["sections"] if section["kind"] == "worldbook_entries")
    assert entry_section["content"][0]["title"] == "海港"
    assert "海港设定" in second_payload
    assert "只该由工具读取的正文" in second_payload
    assert "agent requested exact title" in second_payload


def test_compiler_records_deterministic_recent_memory_truncation():
    snapshot = context_snapshot()
    snapshot["recent_memory"] = "记忆" * 500
    request = ContextCompileRequest(
        session_id="session-1",
        task_id="task-1",
        base_revision=1,
        player_input="我继续前进",
        snapshot=snapshot,
        policy=ContextPolicy(version="budget-v1", token_budget=600),
    )

    compiled = compile_context(request)

    memory = next(section for section in compiled.manifest["sections"] if section["kind"] == "recent_memory")
    assert memory["disposition"] == "truncated"
    assert compiled.manifest["estimated_tokens"] <= 600
    assert compiled.manifest["budget_decisions"] == [{"kind": "recent_memory", "reason": "token budget", "disposition": "truncated"}]
    assert "我继续前进" in json.dumps(compiled.payload, ensure_ascii=False)


def test_compiler_can_truncate_recent_memory_to_empty_at_payload_boundary():
    snapshot = context_snapshot()
    empty_request = ContextCompileRequest(
        session_id="session-1", task_id="task-1", base_revision=1, player_input="输入", snapshot={**snapshot, "recent_memory": ""}, policy=ContextPolicy(version="edge-v1", token_budget=10000)
    )
    minimum_budget = compile_context(empty_request).manifest["estimated_tokens"]
    request = ContextCompileRequest(
        session_id="session-1", task_id="task-1", base_revision=1, player_input="输入", snapshot={**snapshot, "recent_memory": "x" * 1000}, policy=ContextPolicy(version="edge-v1", token_budget=minimum_budget)
    )

    compiled = compile_context(request)

    memory = next(section for section in compiled.manifest["sections"] if section["kind"] == "recent_memory")
    assert memory["content"] == ""
    assert compiled.manifest["estimated_tokens"] <= minimum_budget


def test_exact_title_loader_rejects_partial_or_unknown_title(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / ".worldbook_index.json").write_text(
        json.dumps([{"title": "海港", "section": "## 海港", "usage": "海港设定"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "reference.md").write_text("## 海港\n正文。", encoding="utf-8")

    for title in ("海", "海港 ", "不存在"):
        try:
            load_worldbook_entry(tmp_path, title)
        except ValueError:
            continue
        raise AssertionError(f"{title!r} must not resolve a worldbook entry")


def test_runtime_migrates_ticket_one_database_before_compiling_manifest(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    database_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL);
            CREATE TABLE tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, text TEXT NOT NULL, status TEXT NOT NULL, commit_id TEXT, revision INTEGER NOT NULL);
            CREATE TABLE commits (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL, task_id TEXT NOT NULL UNIQUE, draft TEXT NOT NULL);
            CREATE TABLE projection_checkpoints (commit_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
            INSERT INTO sessions VALUES ('local', 0);
            """
        )

    runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>迁移后回合。</p>"),
    )
    result = runtime.submit(text="我继续", idempotency_key="submit-1")

    assert result.status == "succeeded"
    assert runtime.manifest_for_task(result.task_id, 0)["base_revision"] == 0


def test_runtime_rejects_nonzero_legacy_revision_without_state_snapshot(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    (card_folder / ".initvar.json").write_text(json.dumps({"世界": {"时间": "基线"}}, ensure_ascii=False), encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    database_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL);
            CREATE TABLE tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, text TEXT NOT NULL, status TEXT NOT NULL, commit_id TEXT, revision INTEGER NOT NULL);
            CREATE TABLE commits (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL, task_id TEXT NOT NULL UNIQUE, draft TEXT NOT NULL);
            CREATE TABLE projection_checkpoints (commit_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
            INSERT INTO sessions VALUES ('local', 1);
            """
        )
    runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>不应生成。</p>"),
    )

    try:
        runtime.submit(text="我继续", idempotency_key="submit-1")
    except RuntimeError as error:
        assert str(error) == "revision state snapshot is unavailable"
    else:
        raise AssertionError("unreconstructable legacy state must not be silently compiled")


def test_exact_title_loader_reads_user_worldbook_from_user_memory(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / ".worldbook_index.json").write_text(
        json.dumps([{"title": "{{user}}档案", "section": "## {{user}}档案", "usage": "玩家档案"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "user.md").write_text("## {{user}}档案\n玩家专属正文。", encoding="utf-8")

    entry = load_worldbook_entry(tmp_path, "{{user}}档案")

    assert entry.content == "## {{user}}档案\n玩家专属正文。"


def test_runtime_freezes_session_settings_at_construction(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    settings = {"style": {"name": "初始"}}
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
        session_settings=settings,
    )
    settings["style"]["name"] = "外部修改"

    result = runtime.submit(text="测试", idempotency_key="submit-1")
    manifest = runtime.manifest_for_task(result.task_id, 0)
    section = next(section for section in manifest["sections"] if section["kind"] == "settings")

    assert section["content"] == {"style": {"name": "初始"}}


def test_concurrent_worldbook_loads_respect_per_call_limit(tmp_path):
    card_folder = tmp_path / "card"
    memory = card_folder / "memory"
    memory.mkdir(parents=True)
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    entries = [{"title": f"条目{i}", "section": f"## 条目{i}", "usage": "测试"} for i in range(4)]
    (memory / ".worldbook_index.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    (memory / "reference.md").write_text("\n".join(f"## 条目{i}\n正文{i}" for i in range(4)), encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
        manifest_policy=ContextPolicy(version="limit-v1", token_budget=4000, max_worldbook_loads=2),
    )
    result = runtime.submit(text="测试", idempotency_key="submit-1")
    successes = []

    def load(title):
        try:
            runtime.load_worldbook_for_task(result.task_id, title, "parallel")
            successes.append(title)
        except ValueError:
            pass

    threads = [threading.Thread(target=load, args=(f"条目{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 2


def test_follow_up_manifest_keeps_all_prior_worldbook_loads(tmp_path):
    card_folder = tmp_path / "card"
    memory = card_folder / "memory"
    memory.mkdir(parents=True)
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    entries = [{"title": title, "section": f"## {title}", "usage": "测试"} for title in ("人物关系", "地点规则")]
    (memory / ".worldbook_index.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    (memory / "reference.md").write_text("## 人物关系\n关系正文。\n## 地点规则\n地点正文。", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card_folder, projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
    )
    result = runtime.submit(text="测试", idempotency_key="submit-1")
    runtime.load_worldbook_for_task(result.task_id, "人物关系", "first")
    runtime.compile_follow_up_manifest(result.task_id, "继续")
    runtime.load_worldbook_for_task(result.task_id, "地点规则", "second")
    third = runtime.compile_follow_up_manifest(result.task_id, "继续")

    entries_section = next(section for section in third.manifest["sections"] if section["kind"] == "worldbook_entries")
    assert [entry["title"] for entry in entries_section["content"]] == ["人物关系", "地点规则"]


def test_concurrent_follow_up_compilation_returns_one_manifest(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card_folder, projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
    )
    result = runtime.submit(text="测试", idempotency_key="submit-1")
    manifests = []

    def compile_follow_up():
        manifests.append(runtime.compile_follow_up_manifest(result.task_id, "继续"))

    threads = [threading.Thread(target=compile_follow_up) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(manifests) == 2
    assert len(runtime.manifests_for_task(result.task_id)) == 3


def test_runtime_limits_worldbook_loads_per_call(tmp_path):
    card_folder = tmp_path / "card"
    memory = card_folder / "memory"
    memory.mkdir(parents=True)
    (card_folder / ".initvar.json").write_text("{}", encoding="utf-8")
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")
    entries = [{"title": f"条目{i}", "section": f"## 条目{i}", "usage": "测试"} for i in range(4)]
    (memory / ".worldbook_index.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    (memory / "reference.md").write_text("\n".join(f"## 条目{i}\n正文{i}" for i in range(4)), encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>回合。</p>"),
        manifest_policy=ContextPolicy(version="limit-v1", token_budget=4000, max_worldbook_loads=2),
    )
    result = runtime.submit(text="测试", idempotency_key="submit-1")

    runtime.load_worldbook_for_task(result.task_id, "条目0", "first")
    runtime.load_worldbook_for_task(result.task_id, "条目1", "second")
    try:
        runtime.load_worldbook_for_task(result.task_id, "条目2", "third")
    except ValueError as error:
        assert str(error) == "worldbook load limit exceeded"
    else:
        raise AssertionError("worldbook load limit must be enforced")
