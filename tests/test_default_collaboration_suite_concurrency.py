from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
import airp.default_collaboration_suite as suite_module  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def test_install_once_serializes_installers_for_the_same_workspace(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    workspace = Workspace.from_root(tmp_path / "workspace")
    applications = [
        Application.assemble(static_root=static_root, workspace=workspace)
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda app: app.default_collaboration_suite.install_once(), applications)
        )

    assert sorted(result["installed"] for result in results) == [False, True]
    assert len(applications[0].graph_store.list_graphs()) == 1


def _install_in_process(static_root, workspace_root, start, results):
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(workspace_root),
    )
    start.wait()
    results.put(application.default_collaboration_suite.install_once()["installed"])


def test_install_once_serializes_installers_across_processes(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    workspace_root = tmp_path / "workspace"
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_install_in_process,
            args=(static_root, workspace_root, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
    timed_out = [process for process in processes if process.is_alive()]
    for process in timed_out:
        process.terminate()
        process.join(timeout=5)

    assert timed_out == []
    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(results.get(timeout=1) for _ in processes) == [False, True]
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(workspace_root),
    )
    assert len(application.graph_store.list_graphs()) == 1


def test_install_once_uses_the_windows_locking_contract_when_fcntl_is_unavailable(
    tmp_path, monkeypatch
):
    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(descriptor, mode, size):
            calls.append((mode, size))
            assert descriptor >= 0

    monkeypatch.setattr(suite_module, "_fcntl", None)
    monkeypatch.setattr(suite_module, "_msvcrt", FakeMsvcrt)
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )

    result = application.default_collaboration_suite.install_once()

    assert result["installed"] is True
    assert calls == [(FakeMsvcrt.LK_LOCK, 1), (FakeMsvcrt.LK_UNLCK, 1)]
