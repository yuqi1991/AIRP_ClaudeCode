"""Card data access and compatibility projection storage (pure I/O).

The legacy ``engine.card`` import remains a thin Adapter. Projection roots
are explicit when supplied and otherwise resolved through AIRP environment
configuration during the source-checkout migration.
"""
import json
import re
from pathlib import Path



def _default_projection_root() -> Path:
    import os

    configured = os.environ.get("AIRP_PROJECTION_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    from airp.resources import projection_root

    return projection_root()


STYLES = _default_projection_root()


# ═══ File I/O ═══

def read_chat_log(card_folder):
    path = Path(card_folder) / "chat_log.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def write_chat_log(card_folder, log):
    path = Path(card_folder) / "chat_log.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def read_state(projection_root=None):
    path = Path(projection_root) / "state.js" if projection_root else STYLES / "state.js"
    if not path.exists():
        return (
            'window.STATE = {\n'
            '  world: "", stage: "开局", time: "", location: "", env: "",\n'
            '  quest: "", generatedCount: 0, totalTokens: 0, actions: [],\n'
            '  player: "", hp: 0, hpMax: 0, mp: 0, mpMax: 0, exp: 0, expMax: 0, ed: false,\n'
            '  npcs: []\n'
            '};\n'
        )
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_state(js, card_folder=None, projection_root=None):
    path = Path(projection_root) / "state.js" if projection_root else STYLES / "state.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)
    if card_folder:
        card_js_path = Path(card_folder) / "state.js"
        if card_js_path != path:
            with open(card_js_path, "w", encoding="utf-8") as f:
                f.write(js)


def _get_latest_variables(log):
    """Extract current stat_data from the most recent turn that has variables."""
    for turn in reversed(log):
        variables = turn.get("variables")
        if variables and "stat_data" in variables:
            return variables["stat_data"]
    return {}


def _get_latest_delta(log):
    """Extract delta from the most recent turn."""
    if log:
        variables = log[-1].get("variables")
        if variables and "delta" in variables:
            return variables["delta"]
    return {}


def _get_turn_variables(log):
    """Return per-turn variable snapshots for inline card rendering.
    Returns [{index, stat_data, delta}, ...] for every turn.
    """
    result = []
    for turn in log:
        entry = {"index": turn.get("index", 0)}
        variables = turn.get("variables")
        if variables:
            entry["stat_data"] = variables.get("stat_data", {})
            entry["delta"] = variables.get("delta", {})
        else:
            entry["stat_data"] = {}
            entry["delta"] = {}
        result.append(entry)
    return result


def update_state(**kwargs):
    """Update fields in state.js. Keys: stage, time, location, env, quest, generatedCount, npcs, etc."""
    raw = read_state()
    for key, value in kwargs.items():
        if isinstance(value, str):
            raw = re.sub(rf'(\s+{key}:\s*")[^"]*(")', rf'\g<1>{value}\g<2>', raw)
        elif isinstance(value, (int, float)):
            raw = re.sub(rf'(\s+{key}:\s*)\d+', rf'\g<1>{value}', raw)
        elif isinstance(value, list):
            raw = re.sub(rf'(\s+{key}:\s*)\[.*?\]', lambda m: m.group(1) + json.dumps(value, ensure_ascii=False), raw, flags=re.DOTALL)
    write_state(raw)
