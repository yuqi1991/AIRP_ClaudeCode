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
