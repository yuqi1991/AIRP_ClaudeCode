"""
RP Response Handler — parses Claude Code output and manages chat_log / content.js / state.js.
Also provides reroll and delete-turn logic for the bridge server.
Usage:
  python handler.py <card_folder>          # process response.txt → append turn
  python handler.py <card_folder> --opening # first turn, no user input
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from engine.mvu import extract_commands, execute_commands, compute_current_variables, audit_variables, validate_command, generate_schema, SchemaNode
from engine.card import (read_chat_log, write_chat_log, read_state, write_state,
                         _get_latest_variables, _get_latest_delta, _get_turn_variables, update_state)
from engine.render import (resolve_card_macros, resolve_macros, _stat_color, _stat_max_guess, _render_stat_bar,
                           _html_escape, _build_beautify_panel, _escape_attr, _strip_tags,
                           _strip_mvu_commands, _text_to_p, _extract_options)

STYLES = Path(__file__).parent / "styles"
BRIDGE = "http://localhost:8765"


# ═══ Tag Parsing ═══

def parse_response(text):
    """Parse response.txt into structured parts."""
    result = {}
    for tag in ("polished_input", "content", "summary", "options", "tokens"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        if m:
            raw = m.group(1).strip()
            if tag == "tokens":
                result[tag] = _parse_tokens(raw)
            else:
                result[tag] = raw
    return result


def _parse_tokens(raw):
    """Parse <tokens> block: 'key: value' lines → dict.
    Handles int, float, and percentage (77.4%) values."""
    tokens = {}
    for line in raw.split("\n"):
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            key = k.strip()
            # Try int
            try:
                tokens[key] = int(v)
                continue
            except ValueError:
                pass
            # Try float (includes percentage like "77.4%")
            try:
                v_clean = v.replace("%", "")
                tokens[key] = float(v_clean)
                continue
            except ValueError:
                pass
    return tokens


def write_content_js(card_folder, projection_root=None):
    """Rebuild frontend content.js from chat_log.json."""
    log = read_chat_log(card_folder)

    html_parts = []
    turn_tokens = {}  # { "N": {"in": X, "out": Y, "total": Z}, ... }

    for turn in log:
        ai_raw = turn.get("ai", "")
        user_raw = turn.get("user", "")
        turn_idx = turn.get("index", 0)

        # Strip <options>/<summary>/<tokens> from display
        ai_display = _strip_tags(ai_raw, "options")
        ai_display = _strip_tags(ai_display, "summary")
        ai_display = _strip_tags(ai_display, "tokens")
        # Strip MVU commands (_.set / _.add / _.insert etc.) from display.
        # These are parsed by extract_commands() for variable updates; the
        # card author's regex #1 strips <UpdateVariable> blocks, but bare
        # _.set() lines are the MVU engine's own responsibility.
        ai_display = _strip_mvu_commands(ai_display)
        # Strip hardcoded text colors from inline styles (card authors
        # often bake light-theme colors that become invisible in dark mode)
        ai_display = re.sub(
            r'\bcolor\s*:\s*#[0-9a-fA-F]{3,8}\s*;?\s*',
            '', ai_display,
        )

        # Collect token data for exposure
        tokens = turn.get("tokens")
        if tokens:
            turn_tokens[str(turn_idx)] = tokens

        # Build display wrap for this turn (one per turn)
        wrap = '<div class="turn-wrap">'
        if user_raw:
            # User text is formatted client-side for line breaks and basic
            # Markdown. Escape it before it enters generated content.js.
            user_display = _html_escape(user_raw)
            wrap += '<div class="turn-user"><div class="turn-role">你</div><div class="turn-text">' + user_display + '</div></div>'
        wrap += '<div class="turn-ai"><div class="turn-role">叙事</div><div class="turn-text">' + ai_display + '</div></div>'
        wrap += '</div>'
        html_parts.append(wrap)

    # Extract startup cost from turn 0 token data (persistent across rounds)
    startup_cost = {}
    if log and log[0].get("tokens"):
        t0 = log[0]["tokens"]
        st_in = t0.get("startup_in", 0) or t0.get("in", 0)
        st_out = t0.get("startup_out", 0) or t0.get("out", 0)
        st_total = t0.get("startup_total", 0) or t0.get("total", 0)
        if st_total > 0:
            startup_cost = {
                "in": st_in,
                "out": st_out,
                "total": st_total,
                "cache_hit": t0.get("cache_hit", 0),
            }

    content_html = "".join(html_parts)

    # Load card-specific beautify data if available
    beautify_data = {}
    beautify_path = Path(card_folder) / ".beautify.json"
    if beautify_path.exists():
        try:
            with open(beautify_path, "r", encoding="utf-8") as f:
                beautify_data = json.load(f)
        except Exception:
            pass

    # Load card author's beautify panel template (from regex_scripts).
    # The template is provided as a separate BEAUTIFY_HTML variable so the
    # beautify panel renders independently of narrative content — opening
    # switches and name changes no longer destroy the panel DOM.
    # _st_shims.js (loaded in index.html) provides ST/MVU API shims so the
    # original author script runs unchanged.
    beautify_html = ""
    template_path = Path(card_folder) / ".beautify_template.html"
    if template_path.exists():
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_html = f.read()
            # Strip structural document tags
            template_html = re.sub(
                r'<!doctype[^>]*>', '', template_html, flags=re.IGNORECASE,
            )
            template_html = re.sub(
                r'</?html[^>]*>', '', template_html, flags=re.IGNORECASE,
            )
            template_html = re.sub(
                r'</?head[^>]*>', '', template_html, flags=re.IGNORECASE,
            )
            template_html = re.sub(
                r'</?body[^>]*>', '', template_html, flags=re.IGNORECASE,
            )
            # <script type="module"> → <script> so it runs as classic script
            template_html = template_html.replace(
                '<script type="module">', '<script>'
            )
            # Macros ({{format_message_variable}}, {{getvar}}, etc.) are left INTACT
            # in the template — they are resolved client-side at display time
            # against window.MVU_VARIABLES, matching the real MVU pipeline where
            # the engine resolves macros dynamically on each render cycle.
            beautify_html = template_html
        except Exception:
            pass
    else:
        # No author template — use fallback inline beautify panel
        latest_vars = _get_latest_variables(log)
        latest_delta = _get_latest_delta(log)
        panel_html = _build_beautify_panel(latest_vars, latest_delta, beautify_data)
        if panel_html:
            beautify_html = panel_html

    # Strip <StatusPlaceHolderImpl/> markers from narrative content
    content_html = content_html.replace("<StatusPlaceHolderImpl/>", "")

    latest_summary = log[-1].get("summary", "") if log else ""
    latest_ai = log[-1].get("ai", "") if log else ""

    # Extract options from latest AI content
    opts_match = re.search(r"<options>(.*?)</options>", latest_ai, re.DOTALL)
    options = []
    if opts_match:
        for line in opts_match.group(1).strip().split("\n"):
            line = line.strip()
            if line:
                options.append(line)

    # Load card author's regex_scripts for frontend application
    regex_scripts = []
    regex_path = Path(card_folder) / ".regex_scripts.json"
    if regex_path.exists():
        try:
            with open(regex_path, "r", encoding="utf-8") as f:
                regex_scripts = json.load(f)
        except Exception:
            pass

    js = (
        "window.CONTENT_HTML = " + json.dumps(content_html, ensure_ascii=False) + ";\n"
        "window.BEAUTIFY_HTML = " + json.dumps(beautify_html, ensure_ascii=False) + ";\n"
        "window.SUMMARY_TEXT = " + json.dumps(latest_summary, ensure_ascii=False) + ";\n"
        "window.TURN_OPTIONS = " + json.dumps(options, ensure_ascii=False) + ";\n"
        "window.TURN_TOKENS = " + json.dumps(turn_tokens, ensure_ascii=False) + ";\n"
        "window.STARTUP_COST = " + json.dumps(startup_cost, ensure_ascii=False) + ";\n"
        "window.MVU_VARIABLES = " + json.dumps(_get_latest_variables(log), ensure_ascii=False) + ";\n"
        "window.MVU_DELTA = " + json.dumps(_get_latest_delta(log), ensure_ascii=False) + ";\n"
        "window.TURN_VARIABLES = " + json.dumps(_get_turn_variables(log), ensure_ascii=False) + ";\n"
        "window.BEAUTIFY_DATA = " + json.dumps(beautify_data, ensure_ascii=False) + ";\n"
        "window.REGEX_SCRIPTS = " + json.dumps(regex_scripts, ensure_ascii=False) + ";\n"
    )

    target_root = Path(projection_root) if projection_root else STYLES
    target_root.mkdir(parents=True, exist_ok=True)
    path = target_root / "content.js"
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)

    # Dual write to card folder for per-card frontend
    card_path = Path(card_folder) / "content.js"
    if card_path != path:
        with open(card_path, "w", encoding="utf-8") as f:
            f.write(js)


# ═══ Turn Operations ═══

MVU_SERVER = "http://127.0.0.1:8766"

def _mvu_post(endpoint, data=None):
    """POST to mvu_server, return parsed JSON or None on failure."""
    import urllib.request as _ur
    try:
        body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8")
        req = _ur.Request(f"{MVU_SERVER}/{endpoint}", data=body,
                          headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _validate_commands_via_server(commands):
    """Batch validate commands via mvu_server. Returns (valid_cmds, errors)."""
    if not commands:
        return commands, []
    payload = {"commands": []}
    for cmd in commands:
        item = {"op": cmd.type}
        if cmd.args:
            item["path"] = cmd.args[0] if len(cmd.args) > 0 else None
            item["value"] = cmd.args[1] if len(cmd.args) > 1 else None
        if len(cmd.args) > 2:
            item["extra"] = cmd.args[2]
        payload["commands"].append(item)
    result = _mvu_post("validate_all", payload)
    if result is None or "results" not in result:
        return commands, []  # Server unavailable → allow all
    valid = []
    errors = []
    for i, r in enumerate(result["results"]):
        if r.get("ok"):
            valid.append(commands[i])
        else:
            errors.append({
                "command": commands[i].full_match.strip() if commands[i].full_match else str(commands[i].args),
                "error": r.get("error", "unknown"),
            })
    return valid, errors


def _load_var_schema(card_folder, fallback_data=None):
    """Load variable schema.

    Prefers mvu_server (real Zod schema loaded from card scripts).
    Falls back to .initvar_schema.json → generate_schema() from data.
    """
    # Try mvu_server first
    schema_meta = _mvu_post("schema")
    if schema_meta and schema_meta.get("fields"):
        return _build_schema_from_definition(schema_meta)

    # Fallback: file-based schema
    schema_path = Path(card_folder) / ".initvar_schema.json"
    if schema_path.exists():
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_raw = json.load(f)
            return _build_schema_from_definition(schema_raw)
        except Exception:
            pass

    # Last resort: generate from data
    if fallback_data is None:
        initvar_path = Path(card_folder) / ".initvar.json"
        if initvar_path.exists():
            try:
                with open(initvar_path, "r", encoding="utf-8") as f:
                    fallback_data = json.load(f)
            except Exception:
                pass
    if fallback_data:
        return generate_schema(fallback_data)
    return None


def _build_schema_from_definition(schema_def):
    """Build a SchemaNode tree from Node.js runner's schema definition."""
    fields = schema_def.get("fields", {})
    enums = schema_def.get("enums", {})
    constraints = schema_def.get("constraints", [])

    # Group field paths into a tree structure
    root = {"_children": {}, "_type": "object"}

    for path, info in fields.items():
        parts = path.split(".")
        node = root
        for i, part in enumerate(parts):
            if part == "*":
                # Wildcard = key can be anything
                node["_type"] = "object"
                continue
            if part not in node["_children"]:
                node["_children"][part] = {"_children": {}, "_type": "any"}
            node = node["_children"][part]
            if i == len(parts) - 1:
                node["_type"] = info.get("type", "any")
                node["_nullable"] = info.get("nullable", True)

    # Apply enum constraints
    for enum_path, enum_values in enums.items():
        parts = enum_path.split(".")
        node = root
        for part in parts:
            if part.startswith("_"):
                # _keys / _values are metadata keys
                break
            if part == "*":
                node["_type"] = "object"
                continue
            if part not in node["_children"]:
                node["_children"][part] = {"_children": {}, "_type": "any"}
            node = node["_children"][part]

    # Convert to SchemaNode
    return _dict_to_schema_node(root)


def _dict_to_schema_node(d):
    """Recursively convert dict tree to SchemaNode."""
    node_type = d.get("_type", "any")
    properties = {}
    for k, v in d.get("_children", {}).items():
        properties[k] = _dict_to_schema_node(v)

    schema = SchemaNode(
        type=node_type,
        extensible="*" in d.get("_children", {}),
    )
    if properties:
        schema.properties = properties
    return schema


def append_turn(card_folder, polished_input=None, content="", summary="", options="", is_opening=False, tokens=None, full_text="", projection_root=None):
    """Append a new turn to chat_log and rebuild content.js."""
    log = read_chat_log(card_folder)
    next_index = len(log)

    # ── MVU: Compute current variables ──
    prev_vars = compute_current_variables(log)

    # ── MVU: Load variable schema for validation ──
    var_schema = _load_var_schema(card_folder, prev_vars)

    # ── MVU: Extract commands from full response text ──
    commands = extract_commands(full_text or content)
    # On first turn, try loading .initvar.json as baseline
    if not prev_vars:
        initvar_path = Path(card_folder) / ".initvar.json"
        if initvar_path.exists():
            try:
                with open(initvar_path, "r", encoding="utf-8") as f:
                    prev_vars = json.load(f)
            except Exception:
                pass

    # ── MVU: Validate commands against schema via mvu_server (real Zod) ──
    valid_commands = []
    validation_errors = []
    if commands:
        # Try server-side validation first (real Zod schema)
        valid_commands, validation_errors = _validate_commands_via_server(commands)
        # If server returned nothing (unavailable), fall back to file-based schema
        if not valid_commands and not validation_errors:
            if var_schema:
                for cmd in commands:
                    ok, err = validate_command(cmd, var_schema)
                    if ok:
                        valid_commands.append(cmd)
                    else:
                        validation_errors.append({"command": cmd.full_match.strip() if cmd.full_match else str(cmd.args), "error": err})
            else:
                valid_commands = commands
        if validation_errors:
            for ve in validation_errors:
                print(f"[handler] schema validation: {ve['error']} (command: {ve['command'][:80]})")
    else:
        valid_commands = commands

    new_vars, changes = execute_commands(prev_vars, valid_commands) if valid_commands else (prev_vars, {})
    # Attach validation errors to changes delta
    if validation_errors:
        changes["_validation_errors"] = validation_errors

    # ── Resolve template macros in content ──
    resolved_vars = new_vars if new_vars else prev_vars
    content = resolve_macros(content, resolved_vars)

    ai_text = content
    if summary:
        ai_text += "\n\n<summary>" + summary + "</summary>"
    if options:
        ai_text += "\n\n<options>\n" + options + "\n</options>"

    entry = {"index": next_index, "ai": ai_text, "summary": summary}
    if not is_opening and polished_input:
        entry["user"] = polished_input
    if tokens:
        entry["tokens"] = tokens
    # Store variables if any exist or were changed
    if new_vars:
        entry["variables"] = {"stat_data": new_vars}
        if changes:
            entry["variables"]["delta"] = changes
    # Always carry forward variables from previous turns even if unchanged
    elif prev_vars:
        entry["variables"] = {"stat_data": prev_vars}

    log.append(entry)
    write_chat_log(card_folder, log)
    write_content_js(card_folder, projection_root=projection_root)

    # ── Variable audit: write diff to .var_diff.json for next-turn awareness ──
    try:
        audit = audit_variables(prev_vars or {}, new_vars or {}, content)
        audit_path = Path(card_folder) / ".var_diff.json"
        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # never block turn delivery for audit failure

    # Update state: increment generatedCount and accumulate totalTokens
    state_raw = read_state(projection_root=projection_root)
    new_count = (next_index + 1)
    state_raw = re.sub(r'(\s+generatedCount:\s*)\d+', rf'\g<1>{new_count}', state_raw)
    if tokens:
        turn_total = tokens.get("total") or tokens.get("round_total") or tokens.get("startup_total") or 0
        if turn_total > 0:
            # Accumulate into totalTokens
            m = re.search(r'totalTokens:\s*(\d+)', state_raw)
            prev_total = int(m.group(1)) if m else 0
            new_total = prev_total + turn_total
            state_raw = re.sub(r'(\s+totalTokens:\s*)\d+', rf'\g<1>{new_total}', state_raw)
    write_state(state_raw, card_folder, projection_root=projection_root)

    return next_index


def reroll_last(card_folder):
    """Delete last turn, restore user input for regeneration. Returns the user text."""
    log = read_chat_log(card_folder)
    if not log:
        return None

    last = log[-1]

    # Refuse to reroll an opening (no user field) — nothing to regenerate from
    if not last.get("user"):
        return None

    log.pop()
    write_chat_log(card_folder, log)
    write_content_js(card_folder)

    # Update generatedCount
    state_raw = read_state()
    new_count = len(log) + 2 if log else 1
    state_raw = re.sub(r'(\s+generatedCount:\s*)\d+', rf'\g<1>{new_count}', state_raw)
    write_state(state_raw, card_folder)

    user_text = last.get("user", "")
    (STYLES / "input.txt").write_text(user_text, encoding="utf-8")
    (STYLES / ".pending").touch()
    return user_text


def delete_turns(card_folder, from_index):
    """Delete turns with index >= from_index."""
    log = read_chat_log(card_folder)
    log = [t for t in log if t.get("index", 0) < from_index]
    write_chat_log(card_folder, log)
    write_content_js(card_folder)

    # Update generatedCount and clear pending
    (STYLES / ".pending").unlink(missing_ok=True)
    state_raw = read_state()
    new_count = len(log) + 2 if log else 1
    state_raw = re.sub(r'(\s+generatedCount:\s*)\d+', rf'\g<1>{new_count}', state_raw)
    write_state(state_raw, card_folder)


# ═══ Bridge Calls ═══

def bridge_done():
    try:
        urllib.request.urlopen(BRIDGE + "/api/done")
    except Exception:
        pass


# ═══ Openings Management ═══

OPENINGS_FILE = STYLES / "openings.json"


def _opening_files(card_folder=None):
    paths = []
    if card_folder:
        base = Path(card_folder)
        paths.extend([base / "memory" / "openings.json", base / "openings.json"])
    paths.append(OPENINGS_FILE)
    return paths


def list_openings(card_folder=None):
    """Return card-local openings before the shared compatibility copy."""
    for path in _opening_files(card_folder):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                continue
    return []


def switch_opening(card_folder, opening_id, *, user_name="旅行者", character_name=""):
    """Replace the current opening (index 0) with a different one."""
    openings = list_openings(card_folder)
    target = None
    for o in openings:
        if o["id"] == opening_id:
            target = o
            break
    if not target:
        return False

    log = read_chat_log(card_folder)
    if not log:
        return False

    # Only allow switching the opening (index 0 must be AI-only, no user input)
    if log[0].get("user"):
        return False

    # Replace opening AI content with the selected greeting
    # Convert plain-text paragraphs to <p> tags if not already HTML
    greeting = resolve_card_macros(
        target["content"], user_name=user_name, character_name=character_name
    )
    if "<p>" not in greeting and "<content>" not in greeting:
        greeting = _text_to_p(greeting)

    # Use per-opening options if available, otherwise keep existing
    opts = target.get("options", "")
    if not opts:
        opts = _extract_options(log[0].get("ai", ""))
    opts_block = "\n".join('<font color="#b06a3d">' + o + '</font>' for o in opts) if isinstance(opts, list) else opts if opts else ""

    log[0]["ai"] = "<content>\n" + greeting + "\n</content>\n\n<summary>" + log[0].get("summary", "") + "</summary>\n\n<options>\n" + opts_block + "\n</options>"

    # Apply per-opening variable state if the opening defines one.
    # This matches real MVU behaviour where alternate greetings embed
    # <UpdateVariable> blocks to override [InitVar] baseline values.
    opening_vars = target.get("variables")
    if opening_vars:
        if "variables" not in log[0] or not log[0]["variables"]:
            log[0]["variables"] = {}
        log[0]["variables"]["stat_data"] = opening_vars
        log[0]["variables"]["delta"] = {}

    write_chat_log(card_folder, log)
    write_content_js(card_folder)
    return True


# ═══ CLI ═══

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python handler.py <card_folder> [--opening]")
        sys.exit(1)

    card_folder = sys.argv[1]

    is_opening = "--opening" in sys.argv

    # Read response.txt
    resp_path = STYLES / "response.txt"
    if not resp_path.exists():
        print("[handler] No response.txt found")
        sys.exit(1)

    response_text = resp_path.read_text(encoding="utf-8")
    parts = parse_response(response_text)

    content = parts.get("content", response_text)
    summary = parts.get("summary", "")
    options = parts.get("options", "")
    polished_input = parts.get("polished_input", "")
    tokens = parts.get("tokens", None)

    # ── Opening: compute startup cost BEFORE append_turn so turn 0 has token stats ──
    if is_opening and not tokens:
        try:
            from engine.tokens import save_checkpoint, load_checkpoint
            save_checkpoint(card_folder, label="startup_end")
            cp = load_checkpoint(card_folder)
            startup_cost = cp.get("startup_cost", {})
            st_in = startup_cost.get("input_tokens", 0)
            st_out = startup_cost.get("output_tokens", 0)
            if st_in > 0 or st_out > 0:
                tokens = {
                    "in": st_in,
                    "out": st_out,
                    "total": st_in + st_out,
                    "cache_read": startup_cost.get("cache_read", 0),
                    "cache_hit": startup_cost.get("cache_hit_pct", 0.0),
                    "is_startup": True,
                }
        except Exception:
            pass

    idx = append_turn(
        card_folder,
        polished_input=polished_input if not is_opening else None,
        content=content,
        summary=summary,
        options=options,
        is_opening=is_opening,
        tokens=tokens,
        full_text=response_text,
    )

    # Clean up
    resp_path.unlink(missing_ok=True)
    bridge_done()

    print(f"[handler] Turn {idx} saved. content.js rebuilt.")
