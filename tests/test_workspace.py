from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from urllib.request import Request, urlopen

SRC = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from airp.workspace import Workspace  # noqa: E402
from airp.host.rp.session_runtime import SessionTurnRuntime  # noqa: E402
from airp.server import SessionRuntimeServer  # noqa: E402


def test_workspace_keeps_mutable_data_outside_shipped_resources(tmp_path):
    workspace = Workspace.from_root(tmp_path / "airp-data")

    assert workspace.providers_root == (tmp_path / "airp-data" / "library" / "providers").resolve()
    assert workspace.agents_root == (tmp_path / "airp-data" / "library" / "agents").resolve()
    assert workspace.projects_root == (tmp_path / "airp-data" / "projects").resolve()
    assert workspace.secrets_path == (tmp_path / "airp-data" / "secrets.json").resolve()
    assert "skills" not in str(workspace.root)


def test_workspace_ensure_creates_only_user_data_directories(tmp_path):
    workspace = Workspace.from_root(tmp_path / "airp-data").ensure()

    assert workspace.providers_root.is_dir()
    assert workspace.agents_root.is_dir()
    assert workspace.graphs_root.is_dir()
    assert workspace.worldbooks_root.is_dir()
    assert workspace.projects_root.is_dir()
    assert workspace.sessions_root.is_dir()
    assert workspace.runtime_root.is_dir()
    assert not workspace.secrets_path.exists()


def test_workspace_default_respects_airp_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRP_DATA_DIR", str(tmp_path / "override"))

    assert Workspace.default().root == (tmp_path / "override").resolve()


def test_runtime_server_writes_studio_library_to_workspace(tmp_path):
    styles = tmp_path / "web"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )
    workspace = Workspace.from_root(tmp_path / "workspace")
    with SessionRuntimeServer(runtime, static_root=styles, workspace=workspace) as server:
        request = Request(
            f"{server.base_url}/v1/studio/providers",
            data=json.dumps({"name": "Local", "base_url": "https://example.test"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 201
    assert list(workspace.providers_root.glob("*.json"))
    assert not (styles / "studio" / "providers").exists()
