"""Application composition for AIRP's Studio and host runtime."""

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
    regex_collections: Any = None
    projects: Any = None
    active_graphs: Any = None
    default_collaboration_suite: Any = None
    startup: Any = None

    def initialize(self) -> dict[str, Any]:
        """Run application-owned startup work and return a public safe report."""
        result = (
            self.default_collaboration_suite.install_once()
            if self.default_collaboration_suite is not None
            else {}
        )
        allowed = {"code", "boundary", "message", "action", "project_id"}
        diagnostics = [
            {key: value for key, value in diagnostic.items() if key in allowed}
            for diagnostic in result.get("diagnostics", [])
            if isinstance(diagnostic, dict)
        ]
        resource_keys = {
            "provider": "provider_profile_id",
            "regex": "regex_collection_id",
            "writer": "writer_agent_id",
            "reviewer": "reviewer_agent_id",
            "graph": "graph_id",
        }
        initial_resource_ids = {
            public_name: result[source_name]
            for public_name, source_name in resource_keys.items()
            if isinstance(result.get(source_name), str) and result[source_name]
        }
        self.startup = {
            "ok": True,
            "status": "degraded" if result.get("status") == "degraded" else "success",
            "diagnostics": diagnostics,
            "initial_resource_ids": initial_resource_ids,
        }
        return self.startup


    @classmethod
    def assemble(
        cls,
        *,
        static_root: str | Path | None,
        workspace: Workspace | str | Path | None = None,
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
        from airp.engine.active_graph import ActiveGraphSelectionStore
        from airp.engine.graph_definitions import GraphDefinitionService, GraphDefinitionStore
        from airp.engine.provider_profiles import ProviderProfileService
        from airp.engine.regex_collections import RegexCollectionLibrary
        from airp.engine.secret_store import LocalSecretStore
        from airp.engine.studio_library import ProviderProfileStore
        from airp.engine.worldbook_library import WorldbookLibrary
        from airp.engine.project_library import ProjectLibrary
        from airp.host.rp.tools import TOOL_SCHEMAS
        from airp.default_collaboration_suite import DefaultCollaborationSuite

        root = Path(static_root).resolve()
        data_workspace = None
        if workspace is not None:
            data_workspace = workspace if isinstance(workspace, Workspace) else Workspace.from_root(workspace)
            data_workspace.ensure()
        resolved_graph_root = Path(graph_root).resolve() if graph_root else root / "graphs"

        provider_store = ProviderProfileStore(root, workspace=data_workspace)
        secret_path = data_workspace.secrets_path if data_workspace else root / "studio" / "secrets.json"
        secret_store = LocalSecretStore(secret_path)
        provider_service = ProviderProfileService(provider_store, secret_store)
        agent_store = AgentDefinitionStore(
            root,
            graph_root=resolved_graph_root,
            workspace=data_workspace,
            tool_schema_provider=TOOL_SCHEMAS,
        )
        graph_store = GraphDefinitionStore(root, agent_store=agent_store, workspace=data_workspace)
        worldbooks = WorldbookLibrary(root, workspace=data_workspace)
        def regex_collection_references(collection_id: str) -> list[dict[str, str]]:
            references: list[dict[str, str]] = []
            try:
                agents = agent_store.list_agents()
            except Exception:
                return references
            for agent in agents:
                if not isinstance(agent, dict) or agent.get("regex_collection_id") != collection_id:
                    continue
                agent_id = agent.get("agent_id") or agent.get("id")
                if not isinstance(agent_id, str):
                    continue
                references.append(
                    {
                        "type": "agent",
                        "id": agent_id,
                        "name": str(agent.get("name") or agent_id),
                    }
                )
            return references

        regex_collections = RegexCollectionLibrary(
            root,
            workspace=data_workspace,
            reference_callback=regex_collection_references,
        )
        projects = ProjectLibrary(root, worldbooks=worldbooks, workspace=data_workspace)
        active_graphs = ActiveGraphSelectionStore(data_workspace)
        default_collaboration_suite = (
            DefaultCollaborationSuite(
                workspace=data_workspace,
                provider_store=provider_store,
                secret_store=secret_store,
                regex_collections=regex_collections,
                agent_store=agent_store,
                graph_store=graph_store,
                projects=projects,
                active_graphs=active_graphs,
            )
            if data_workspace is not None
            else None
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
            regex_collections=regex_collections,
            projects=projects,
            active_graphs=active_graphs,
            default_collaboration_suite=default_collaboration_suite,
        )
