from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from airp.engine.active_graph import ActiveGraphSelectionError, ActiveGraphSelectionStore
from airp.workspace import Workspace


def test_select_if_unset_selects_an_unconfigured_project_in_memory():
    store = ActiveGraphSelectionStore()

    assert store.select_if_unset("story", "default") is True
    assert store.graph_id_for("story") == "default"


def test_select_if_unset_has_one_winner_in_memory():
    store = ActiveGraphSelectionStore()
    graph_ids = [f"graph-{index}" for index in range(8)]
    barrier = threading.Barrier(len(graph_ids))

    def select(graph_id: str) -> tuple[str, bool]:
        barrier.wait()
        return graph_id, store.select_if_unset("story", graph_id)

    with ThreadPoolExecutor(max_workers=len(graph_ids)) as pool:
        results = list(pool.map(select, graph_ids))

    winners = [graph_id for graph_id, selected in results if selected]
    assert len(winners) == 1
    assert store.graph_id_for("story") == winners[0]


def test_select_if_unset_has_one_winner_across_file_backed_store_instances(tmp_path):
    workspace = Workspace.from_root(tmp_path / "workspace")
    graph_ids = [f"graph-{index}" for index in range(8)]
    barrier = threading.Barrier(len(graph_ids))

    def select(graph_id: str) -> tuple[str, bool]:
        store = ActiveGraphSelectionStore(workspace)
        barrier.wait()
        return graph_id, store.select_if_unset("story", graph_id)

    with ThreadPoolExecutor(max_workers=len(graph_ids)) as pool:
        results = list(pool.map(select, graph_ids))

    winners = [graph_id for graph_id, selected in results if selected]
    assert len(winners) == 1
    assert ActiveGraphSelectionStore(workspace).graph_id_for("story") == winners[0]


@pytest.mark.parametrize(
    "persisted",
    [
        "{",
        "[]",
        '{"story": 7}',
    ],
)
def test_select_if_unset_fails_closed_for_corrupt_file_backed_data(tmp_path, persisted):
    workspace = Workspace.from_root(tmp_path / "workspace").ensure()
    workspace.active_graph_selections_path.write_text(persisted, encoding="utf-8")
    store = ActiveGraphSelectionStore(workspace)

    with pytest.raises(ActiveGraphSelectionError) as caught:
        store.select_if_unset("story", "default")

    assert caught.value.code == "active_graph_selection_unreadable"


def test_select_if_unset_reports_write_failure_without_changing_selections(tmp_path):
    workspace = Workspace.from_root(tmp_path / "workspace").ensure()
    store = ActiveGraphSelectionStore(workspace)
    store.select("existing-story", "chosen")
    workspace.runtime_root.chmod(0o500)

    try:
        with pytest.raises(ActiveGraphSelectionError) as caught:
            store.select_if_unset("new-story", "default")
    finally:
        workspace.runtime_root.chmod(0o700)

    assert caught.value.code == "active_graph_selection_write_failed"
    assert store.graph_id_for("existing-story") == "chosen"
    assert store.graph_id_for("new-story") is None


def test_select_if_unset_preserves_an_existing_selection_in_both_modes(tmp_path):
    stores = [
        ActiveGraphSelectionStore(),
        ActiveGraphSelectionStore(Workspace.from_root(tmp_path / "workspace")),
    ]

    for store in stores:
        store.select("story", "chosen")
        assert store.select_if_unset("story", "default") is False
        assert store.select_if_unset("story", "other") is False
        assert store.graph_id_for("story") == "chosen"


def test_select_if_unset_accepts_a_legal_empty_file_backed_mapping(tmp_path):
    workspace = Workspace.from_root(tmp_path / "workspace-empty").ensure()
    workspace.active_graph_selections_path.write_text("{}", encoding="utf-8")
    store = ActiveGraphSelectionStore(workspace)

    assert store.select_if_unset("story", "default") is True
    assert store.graph_id_for("story") == "default"
