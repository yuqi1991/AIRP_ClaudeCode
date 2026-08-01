"""Static regression checks for the game workspace extension contract."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "airp" / "web"


def test_game_page_exposes_stable_workspace_regions_and_slots():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert '<body data-airp-app="game-workspace">' in page
    assert 'data-airp-region="topbar"' in page
    assert 'data-airp-region="navigation"' in page
    assert 'data-airp-region="workspace"' in page
    assert 'data-airp-region="conversation"' in page
    assert 'data-airp-region="composer"' in page
    assert 'data-airp-region="monitor"' in page
    assert 'data-airp-region="studio-drawer"' in page
    assert 'data-airp-region="node-debug-overlay"' in page
    assert 'data-airp-slot="monitor-session"' in page
    assert 'data-airp-slot="monitor-runtime"' in page
    assert 'data-airp-slot="monitor-graph"' in page
    assert 'data-airp-slot="composer-input"' in page
    assert 'data-airp-slot="debug-body"' in page


def test_game_page_loads_workspace_contract_and_visual_tokens():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    contract = (WEB_ROOT / "game-workspace-contract.js").read_text(encoding="utf-8")
    tokens = (WEB_ROOT / "game-workspace.css").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="game-workspace.css">' in page
    assert '<script src="game-workspace-contract.js"></script>' in page
    assert "version: 1" in contract
    assert "studioDrawer" in contract
    assert "nodeDebug" in contract
    assert "--airp-color-bg" in tokens
    assert "--airp-motion-panel" in tokens


def test_workspace_contract_exports_state_and_event_boundaries():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    contract = (WEB_ROOT / "game-workspace-contract.js").read_text(encoding="utf-8")

    for name in (
        "setState",
        "patchState",
        "emit",
        "region",
        "slot",
    ):
        assert name in contract

    for event_name in (
        "conversation:rendered",
        "monitor:trace-rendered",
        "node-debug:opened",
    ):
        assert event_name in page


def test_monitor_has_vertical_graph_observation_and_status_contract():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    tokens = (WEB_ROOT / "game-workspace.css").read_text(encoding="utf-8")

    assert 'data-airp-slot="monitor-session"' in page
    assert 'data-airp-slot="monitor-runtime"' in page
    assert 'class="monitor-topology"' in page
    assert "monitor-topology-list" in page
    assert "monitor-node-running" in tokens
    assert "monitor-node-succeeded" in tokens
    assert "monitor-node-failed" in tokens
    assert "is-active" in page
    assert "NODE_STATUS_LABELS" in page
    assert "graph.node.started" in page
    assert "graph.node.finished" in page
    assert "Graph Observation" in page


def test_monitor_debug_overlay_keeps_live_trace_model_and_tool_sections():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'data-airp-region="node-debug-overlay"' in page
    assert 'data-airp-slot="debug-body"' in page
    assert 'id="node-debug-model-calls"' in page
    assert 'id="node-debug-tool-calls"' in page
    assert 'id="node-debug-input"' in page
    assert "/v1/studio/node-runs/" in page
    assert "/v1/session/agent-traces" in page
    assert "scheduleNodeDetailRefresh" in page
    assert "tool_calls" in page
    assert "model_calls" in page
def test_agents_orchestration_drawer_exposes_linear_editor_contract():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "studio-agents-drawer.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "game-workspace.css").read_text(encoding="utf-8")

    assert '<script src="studio-agents-drawer.js"></script>' in page
    for marker in (
        'data-studio-drawer-view="agents"',
        'data-studio-drawer-view="orchestration"',
        'id="studio-agent-search"',
        'id="studio-agent-instruction"',
        'id="studio-agent-preview"',
        'id="studio-agent-model"',
        'id="studio-agent-tools"',
        'id="studio-agent-regex"',
        'id="studio-graph-topology"',
        'id="studio-node-enabled"',
        'id="studio-graph-output"',
    ):
        assert marker in page
    for api_path in (
        "/v1/studio/agents",
        "/prompt-preview",
        "/v1/studio/regex-collections",
        "/v1/studio/graphs",
        "/v1/session/runtime/graph",
    ):
        assert api_path in script
    for behavior in (
        "agentFilter",
        "compilePreview",
        "finalEnabledNodeId",
        "ensureValidOutput",
        "studio:runtime-graph-selected",
    ):
        assert behavior in script
    assert ".studio-topology-node::after" in styles


def test_game_page_mounts_worldbook_definition_drawer_and_api_actions():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "worldbook-drawer.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "worldbook-drawer.css").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="worldbook-drawer.css">' in page
    assert '<script src="worldbook-drawer.js"></script>' in page
    assert 'id="studio-drawer-host"' in page
    assert 'id="studio-link"' in page
    for control in (
        "worldbook-drawer-library-search",
        "worldbook-drawer-import",
        "worldbook-drawer-new",
        "worldbook-drawer-copy",
        "worldbook-drawer-rename",
        "worldbook-drawer-delete",
        "worldbook-drawer-export",
        "worldbook-drawer-entry-search",
        "worldbook-drawer-entry-sort",
        "worldbook-drawer-save-bindings",
    ):
        assert control in script
    for endpoint in (
        "/v1/studio/worldbooks",
        "/import",
        "/copy",
        "/export",
        "/v1/studio/projects/",
        "/worldbooks",
    ):
        assert endpoint in script
    assert "worldbook-drawer-panel" in styles
    assert "worldbook-drawer-body" in styles
    assert "worldbook-entry-card" in styles


def test_model_drawer_exposes_redacted_provider_profile_contract():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "studio-model-drawer.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "game-workspace.css").read_text(encoding="utf-8")

    assert '<script src="studio-model-drawer.js"></script>' in page
    for marker in (
        'id="studio-model-toggle"',
        'data-studio-drawer-view="model"',
        'id="studio-model-view"',
        'id="studio-provider-list"',
        'id="studio-provider-format"',
        'id="studio-provider-api-key"',
        'id="studio-provider-key-status"',
        'id="studio-provider-models"',
        'id="studio-provider-test"',
        'id="studio-provider-refresh"',
        'id="studio-provider-delete-key"',
    ):
        assert marker in page
    for api_path in (
        "/v1/studio/providers",
        "/test",
        "/models/refresh",
        "/secret",
    ):
        assert api_path in script
    for behavior in (
        "key_configured",
        "profilePayload",
        "testConnection",
        "refreshModels",
        "deleteKey",
        "model_discovery",
        "API key 不会回显",
    ):
        assert behavior in script or behavior in page
    assert ".studio-provider-grid" in styles
