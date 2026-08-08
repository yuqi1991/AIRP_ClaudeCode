from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
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
