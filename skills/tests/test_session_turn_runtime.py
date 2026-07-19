import json
import sys
import threading
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime


def write_card_fixture(card_folder):
    (card_folder / ".initvar.json").write_text(
        json.dumps({"世界": {"时间": "1月1日 09:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (card_folder / "chat_log.json").write_text("[]", encoding="utf-8")


def test_submit_commits_one_turn_and_writes_compatible_projection(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(
            content="<p>海风掠过礁石。</p>",
            summary="玩家来到海边",
            options="<font color=\"#5a7a5a\">继续观察海面</font>",
        ),
    )

    result = runtime.submit(
        text="我走向海边",
        idempotency_key="submit-1",
    )

    assert result.status == "succeeded"
    assert result.revision == 1
    assert result.task_id
    assert result.commit_id

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert log[0]["user"] == "我走向海边"
    assert log[0]["summary"] == "玩家来到海边"

    content = (tmp_path / "projection" / "content.js").read_text(encoding="utf-8")
    state = (tmp_path / "projection" / "state.js").read_text(encoding="utf-8")
    assert "海风掠过礁石" in content
    assert "继续观察海面" in content
    assert "generatedCount: 1" in state

    events = runtime.events_after(0)
    assert [event.type for event in events] == [
        "player_message.submitted",
        "task.queued",
        "task.running",
        "turn.committed",
        "task.succeeded",
    ]
    assert runtime.active_revision() == 1
    assert runtime.task(result.task_id).status == "succeeded"


def test_duplicate_submit_returns_existing_commit_without_duplicate_turn(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>第一回合。</p>"),
    )

    first = runtime.submit(text="我敲门", idempotency_key="submit-1")
    duplicate = runtime.submit(text="我敲门", idempotency_key="submit-1")

    assert duplicate.task_id == first.task_id
    assert duplicate.commit_id == first.commit_id
    assert duplicate.revision == 1

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert len(log) == 1
    assert [event.type for event in runtime.events_after(0)].count("turn.committed") == 1


def test_runtime_restart_uses_durable_revision_and_exposes_commit(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)
    database_path = tmp_path / "runtime.sqlite3"
    projection_root = tmp_path / "projection"

    first_runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>第一回合。</p>"),
    )
    first = first_runtime.submit(text="我推开门", idempotency_key="submit-1")

    restarted_runtime = SessionTurnRuntime(
        database_path=database_path,
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>第二回合。</p>"),
    )
    second = restarted_runtime.submit(text="我走进去", idempotency_key="submit-2")

    assert first.revision == 1
    assert second.revision == 2
    assert restarted_runtime.commit_for_revision(1).id == first.commit_id
    assert restarted_runtime.commit_for_revision(2).task_id == second.task_id

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert [turn["user"] for turn in log] == ["我推开门", "我走进去"]


def test_submit_preserves_mvu_updates_in_legacy_card_projection(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)

    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(
            content="<p>钟声响起。</p>\n_.set('世界.时间', '1月1日 10:00');"
        ),
    )

    result = runtime.submit(text="我等待钟声", idempotency_key="submit-1")

    assert result.status == "succeeded"
    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert log[0]["variables"]["stat_data"]["世界"]["时间"] == "1月1日 10:00"
    assert "_.set" not in (tmp_path / "projection" / "content.js").read_text(encoding="utf-8")


def test_projection_failure_keeps_commit_pending_without_writing_card_turn(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)
    projection_root = tmp_path / "projection"
    projection_root.write_text("not a directory", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=projection_root,
        executor=FakeNarrativeExecutor(content="<p>不会提交到卡片。</p>"),
    )

    try:
        runtime.submit(text="我等待", idempotency_key="submit-1")
    except FileExistsError:
        pass
    else:
        raise AssertionError("projection failure must be visible")

    log = json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))
    assert log == []
    commit = runtime.commit_for_revision(1)
    assert commit is not None
    assert runtime.projection_checkpoint(commit.id) == "pending"


def test_concurrent_duplicate_submit_commits_one_turn(tmp_path):
    card_folder = tmp_path / "card"
    card_folder.mkdir()
    write_card_fixture(card_folder)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card_folder,
        projection_root=tmp_path / "projection",
        executor=FakeNarrativeExecutor(content="<p>唯一回合。</p>"),
    )
    results = []

    def submit():
        results.append(runtime.submit(text="我敲门", idempotency_key="submit-1"))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert {result.task_id for result in results}
    assert len({result.task_id for result in results}) == 1
    assert len(json.loads((card_folder / "chat_log.json").read_text(encoding="utf-8"))) == 1
