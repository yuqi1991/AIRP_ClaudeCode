"""Application composition for the transitional AIRP runtime.

The facade owns object-graph assembly. Transport code can consume these named
objects without knowing where Studio files, secrets or compatibility config
are stored. The lazy imports keep this package usable before the legacy engine
has completed its move into ``src/airp``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airp.workspace import Workspace


@dataclass
class Application:
    workspace: Workspace | None
    provider_profile_store: Any = None
    provider_secret_store: Any = None
    provider_profiles: Any = None
    agent_store: Any = None
    agent_definitions: Any = None
    graph_store: Any = None
    graph_definitions: Any = None
    worldbooks: Any = None
    projects: Any = None
    config_store: Any = None

    @classmethod
    def assemble(
        cls,
        *,
        static_root: str | Path | None,
        workspace: Workspace | str | Path | None = None,
        preset_root: str | Path | None = None,
        graph_root: str | Path | None = None,
    ) -> "Application":
        if static_root is None:
            data_workspace = None
            if workspace is not None:
                data_workspace = (
                    workspace
                    if isinstance(workspace, Workspace)
                    else Workspace.from_root(workspace)
                )
                data_workspace.ensure()
            return cls(workspace=data_workspace)

        from airp.engine.agent_definitions import AgentDefinitionService, AgentDefinitionStore
        from airp.engine.graph_definitions import GraphDefinitionService, GraphDefinitionStore
        from airp.engine.provider_profiles import ProviderProfileService
        from airp.engine.runtime_config import RuntimeConfigStore
        from airp.engine.secret_store import LocalSecretStore
        from airp.engine.studio_library import ProviderProfileStore
        from airp.engine.worldbook_library import WorldbookLibrary
        from airp.engine.project_library import ProjectLibrary

        root = Path(static_root).resolve()
        data_workspace = None
        if workspace is not None:
            data_workspace = workspace if isinstance(workspace, Workspace) else Workspace.from_root(workspace)
            data_workspace.ensure()
        resolved_preset_root = Path(preset_root).resolve() if preset_root else root / "presets"
        resolved_graph_root = Path(graph_root).resolve() if graph_root else root / "graphs"

        provider_store = ProviderProfileStore(root, workspace=data_workspace)
        secret_path = data_workspace.secrets_path if data_workspace else root / "studio" / "secrets.json"
        secret_store = LocalSecretStore(secret_path)
        provider_service = ProviderProfileService(provider_store, secret_store)
        agent_store = AgentDefinitionStore(
            root,
            graph_root=resolved_graph_root,
            preset_root=resolved_preset_root,
            workspace=data_workspace,
        )
        graph_store = GraphDefinitionStore(root, agent_store=agent_store, workspace=data_workspace)
        worldbooks = WorldbookLibrary(root, workspace=data_workspace)
        projects = ProjectLibrary(root, worldbooks=worldbooks, workspace=data_workspace)
        config_store = RuntimeConfigStore(
            root,
            preset_root=resolved_preset_root,
            graph_root=resolved_graph_root,
        )
        return cls(
            workspace=data_workspace,
            provider_profile_store=provider_store,
            provider_secret_store=secret_store,
            provider_profiles=provider_service,
            agent_store=agent_store,
            agent_definitions=AgentDefinitionService(agent_store),
            graph_store=graph_store,
            graph_definitions=GraphDefinitionService(graph_store),
            worldbooks=worldbooks,
            projects=projects,
            config_store=config_store,
        )
