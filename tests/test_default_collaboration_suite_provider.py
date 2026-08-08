from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def test_install_once_reuses_a_configured_deepseek_profile_without_mutating_it(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    legacy = application.provider_profile_store.create_profile(
        {
            "id": "legacy-deepseek",
            "name": "Legacy DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "api_format": "chat_completions",
            "enabled": True,
            "model_ids": ["a-user-model"],
        }
    )
    configured = application.provider_profile_store.create_profile(
        {
            "id": "my-provider",
            "name": "My DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_format": "chat_completions",
            "enabled": True,
            "model_ids": ["another-model"],
        }
    )
    application.provider_secret_store.set("my-provider", "existing-secret")

    result = application.default_collaboration_suite.install_once()

    assert result["provider_profile_id"] == "my-provider"
    assert application.provider_profile_store.get_profile("my-provider") == configured
    assert application.provider_profile_store.get_profile("legacy-deepseek") == legacy
    assert application.provider_secret_store.get("my-provider") == "existing-secret"
    assert len(application.provider_profile_store.list_profiles()) == 2
    assert application.agent_store.get_agent(result["writer_agent_id"])["model_id"] == "deepseek-v4-flash"
    assert application.agent_store.get_agent(result["reviewer_agent_id"])["model_id"] == "deepseek-v4-flash"
