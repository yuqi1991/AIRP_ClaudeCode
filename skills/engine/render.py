"""engine.render — 纯渲染层（content.js / beautify panel 的 HTML 构造）。

从 handler.py 剪切的渲染函数，保持函数名与签名不变。
handler.py 通过 `from engine.render import ...` 引入同名符号，调用点无需修改。

全部为纯函数：接受数据返回 HTML/文本，不读写文件。
内部互相调用（_build_beautify_panel → _render_stat_bar → _stat_color 等）保持不变。
"""
import json
import re


def resolve_macros(text, stat_data):
    """Replace {{getvar::path}} and {{formatvar::path}} macros with variable values.

    {{getvar::玩家.姓名}}   → renders the scalar value directly
    {{formatvar::互动对象}}  → renders nested dict as indented YAML/JSON block
    """
    import re as _re

    def _resolve(path_str):
        keys = path_str.split(".")
        current = stat_data
        for k in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(k)
        return current

    def _format_val(v):
        if v is None:
            return "(未定义)"
        if isinstance(v, (int, float, bool, str)):
            return str(v)
        if isinstance(v, (dict, list)):
            try:
                import yaml
                return yaml.dump(v, allow_unicode=True, default_flow_style=False).strip()
            except ImportError:
                return json.dumps(v, ensure_ascii=False, indent=2)
        return str(v)

    # {{getvar::path}}
    text = _re.sub(
        r"\{\{getvar::([^}]+)\}\}",
        lambda m: _format_val(_resolve(m.group(1).strip())),
        text,
    )

    # {{formatvar::path}}
    text = _re.sub(
        r"\{\{formatvar::([^}]+)\}\}",
        lambda m: _format_val(_resolve(m.group(1).strip())),
        text,
    )

    # {{format_message_variable::stat_data.XXX}} — SillyTavern macro for beautify panel
    text = _re.sub(
        r"\{\{format_message_variable::stat_data\.([^}]+)\}\}",
        lambda m: _format_val(_resolve(m.group(1).strip())),
        text,
    )

    # {{format_message_variable::XXX}} without stat_data prefix (resolve from root)
    text = _re.sub(
        r"\{\{format_message_variable::([^}]+)\}\}",
        lambda m: _format_val(_resolve(m.group(1).strip())),
        text,
    )

    return text


def _stat_color(name):
    """Map stat names to bar colors."""
    n = name.lower()
    if '悔恨' in n: return '#b0624a'
    if '情欲' in n or '情慾' in n: return '#d4948a'
    if '屈从' in n or '屈從' in n: return '#c49a56'
    if '献身' in n or '獻身' in n: return '#9a7aaa'
    if 'hp' in n or '血' in n: return '#b0624a'
    if 'mp' in n or '魔' in n or '蓝' in n: return '#5a8a9a'
    if 'exp' in n or '经验' in n: return '#cc9a56'
    return '#5a7a5a'


def _stat_max_guess(val):
    """Guess a sensible max for a stat value to normalize bar width."""
    if val <= 10: return 10
    if val <= 50: return 50
    if val <= 100: return 100
    mag = 10 ** (len(str(int(val))) - 1)
    import math
    return int(math.ceil(val / mag) * mag)


def _render_stat_bar(label, val, max_val=None):
    """Render a single stat bar as inline HTML."""
    if max_val is None:
        max_val = _stat_max_guess(val)
    pct = min(100, round(val / max_val * 100))
    color = _stat_color(label)
    return (
        '<div class="tv-stat-row">'
        '<span class="tv-stat-label">' + label + '</span>'
        '<div class="tv-stat-bar-bg"><div class="tv-stat-bar-fill" style="width:'
        + str(pct) + '%;background:' + color + '"></div></div>'
        '<span class="tv-stat-value">' + str(val) + '</span>'
        '</div>'
    )


def _html_escape(text):
    """Minimal HTML escaping."""
    if not isinstance(text, str):
        text = str(text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _build_beautify_panel(stat_data, delta, beautify_data):
    """Build the full inline beautify panel HTML from latest variables.

    Returns a complete HTML string to be appended after all turn-wrap divs.
    Supports phone_data from tavern_helper for rich theme rendering
    (avatars, backgrounds, fonts, user profile).
    """
    if not stat_data:
        return ''

    bd = beautify_data or {}
    phone = bd.get('phone_data', {})
    panel_title = bd.get('panel_title', '') or phone.get('user', {}).get('name', '') or ''
    user_name = bd.get('user_name', '') or phone.get('user', {}).get('name', '')
    user_avatar = bd.get('user_avatar', '') or phone.get('user', {}).get('avatar', '')
    panel_bg = bd.get('panel_bg', '') or phone.get('user', {}).get('phoneBg', '')
    panel_font = bd.get('panel_font', '') or phone.get('user', {}).get('font', '')
    fonts = bd.get('fonts', []) or phone.get('fonts', [])
    random_avatars = bd.get('randomAvatars', []) or phone.get('randomAvatars', [])

    # Separate world metadata from characters
    world_data = stat_data.get('世界', {})
    # Character keys — main cast (with sub-objects) come first, NPCs last
    char_keys = []
    npc_keys = []
    for k in stat_data:
        if k == '世界':
            continue
        v = stat_data[k]
        if isinstance(v, dict):
            has_subs = any(isinstance(sv, dict) for sv in v.values())
            if has_subs:
                char_keys.append(k)
            else:
                npc_keys.append(k)
    ordered_keys = char_keys + npc_keys

    # ---- Font CSS (load from phone_data fonts list) ----
    font_css = ''
    if fonts:
        for f in fonts:
            fname = f.get('name', '')
            furl = f.get('url', '')
            if furl:
                font_css += '@import url(' + _html_escape(furl) + ');\n'

    # ---- Panel background style ----
    bg_style = ''
    if panel_bg:
        bg_style = 'background-image:url(' + _html_escape(panel_bg) + ');background-size:cover;background-position:center;'

    # ---- Tabs ----
    tabs_html = ''
    all_tabs = []
    if world_data:
        all_tabs.append(('世界', '世界'))

    for i, ck in enumerate(ordered_keys):
        # Assign avatar round-robin from randomAvatars if available
        all_tabs.append((ck, ck))

    for i, (tab_id, tab_label) in enumerate(all_tabs):
        active = ' active' if i == 0 else ''
        # Avatar icon for character tabs
        avatar_html = ''
        if tab_id != '世界' and random_avatars:
            av_idx = (i - (1 if world_data else 0)) % len(random_avatars)
            avatar_html = '<span class="beautify-tab-avatar" style="background-image:url(' + _html_escape(random_avatars[av_idx]) + ')"></span>'
        tabs_html += '<button class="beautify-tab-btn' + active + '" data-tab="' + _html_escape(tab_id) + '">' + avatar_html + '<span>' + _html_escape(tab_label) + '</span></button>'

    # ---- Tab body ----
    body_html = ''

    # World tab
    if world_data:
        body_html += '<div class="beautify-tab-panel" data-tab="世界">'
        body_html += '<div class="beautify-info-grid">'
        for key in world_data:
            val = world_data[key]
            body_html += '<div class="beautify-info-card"><div class="beautify-info-label">' + _html_escape(key) + '</div><div class="beautify-info-value">' + _html_escape(str(val)) + '</div></div>'
        body_html += '</div></div>'

    # Character tabs
    for ci, ck in enumerate(ordered_keys):
        cd = stat_data[ck]
        is_npc = ck in npc_keys
        body_html += '<div class="beautify-tab-panel" data-tab="' + _html_escape(ck) + '">'

        # ---- Character card header with avatar ----
        av_idx = ci % len(random_avatars) if random_avatars else -1
        char_avatar = random_avatars[av_idx] if av_idx >= 0 else ''

        body_html += '<div class="beautify-char-card">'

        # Avatar
        if char_avatar:
            body_html += '<div class="beautify-char-avatar-wrap"><div class="beautify-char-avatar" style="background-image:url(' + _html_escape(char_avatar) + ')" onclick="zoomPortrait(this)" title="点击放大"></div></div>'

        # Info column
        body_html += '<div class="beautify-char-info">'
        body_html += '<div class="beautify-char-name">' + _html_escape(ck) + '</div>'

        # Current condition
        if cd.get('当前状况'):
            body_html += '<div class="beautify-char-condition">' + _html_escape(str(cd['当前状况'])) + '</div>'

        # Stat bars
        stat_items = [(k, v) for k, v in cd.items() if isinstance(v, (int, float))]
        if stat_items:
            body_html += '<div class="beautify-stat-bars">'
            for skey, sval in stat_items:
                body_html += _render_stat_bar(skey, sval)
            body_html += '</div>'

        # Pregnancy / stage badges
        badges_html = ''
        if cd.get('是否受孕'):
            badges_html += '<span class="beautify-badge badge-pregnant">孕</span>'
        if cd.get('当前阶段'):
            badges_html += '<span class="beautify-badge badge-stage">阶段 ' + _html_escape(str(cd['当前阶段'])) + '</span>'
        if badges_html:
            body_html += '<div class="beautify-badges">' + badges_html + '</div>'

        body_html += '</div>'  # end char-info
        body_html += '</div>'  # end char-card

        # ---- Sub-objects: 着装 + 身体状况 side by side ----
        outfit = cd.get('着装', {})
        body_stats = cd.get('身体状况', {})
        if outfit or body_stats:
            body_html += '<div class="beautify-sub-grid">'
            if outfit:
                body_html += '<div class="beautify-sub-card"><div class="beautify-sub-title">着装</div>'
                for sk, sv in outfit.items():
                    body_html += '<div class="beautify-sub-row"><span class="beautify-sub-key">' + _html_escape(sk) + '</span><span class="beautify-sub-val">' + _html_escape(str(sv)) + '</span></div>'
                body_html += '</div>'
            if body_stats:
                body_html += '<div class="beautify-sub-card"><div class="beautify-sub-title">身体</div>'
                for sk, sv in body_stats.items():
                    body_html += '<div class="beautify-sub-row"><span class="beautify-sub-key">' + _html_escape(sk) + '</span><span class="beautify-sub-val">' + _html_escape(str(sv)) + '</span></div>'
                body_html += '</div>'
            body_html += '</div>'

        # Other dict sub-objects (not 着装/身体状况)
        for key, val in cd.items():
            if isinstance(val, dict) and key not in ('着装', '身体状况'):
                body_html += '<details class="beautify-sub"><summary>' + _html_escape(key) + '</summary>'
                for sk, sv in val.items():
                    body_html += '<div class="beautify-sub-row"><span class="beautify-sub-key">' + _html_escape(sk) + '</span><span class="beautify-sub-val">' + _html_escape(str(sv)) + '</span></div>'
                body_html += '</details>'

        # Flat status fields — fallback for cards whose characters are flat
        # dicts (no 当前状况 / no 着装 sub-objects). Surface dynamic fields
        # (当前*/近期*/最近*/短期*/当日*) as a KV list; static description
        # fields (body, outfit, identity) stay hidden, they belong in the
        # worldbook reference rather than the live status panel.
        status_fields = [(k, v) for k, v in cd.items()
                         if not isinstance(v, dict)
                         and k not in ('当前状况', '当前阶段', '是否受孕')
                         and (k.startswith('当前') or k.startswith('近期')
                              or k.startswith('最近') or k.startswith('短期')
                              or k.startswith('当日'))]
        if status_fields:
            body_html += '<div class="beautify-flat-list">'
            for fk, fv in status_fields:
                body_html += '<div class="beautify-sub-row"><span class="beautify-sub-key">' + _html_escape(fk) + '</span><span class="beautify-sub-val">' + _html_escape(str(fv)) + '</span></div>'
            body_html += '</div>'

        # Delta changes
        char_delta = {}
        for dk, dv in (delta or {}).items():
            if dk.startswith(ck + '.'):
                short_key = dk[len(ck) + 1:]
                char_delta[short_key] = dv

        if char_delta:
            body_html += '<div class="beautify-delta">'
            for dk, dv in char_delta.items():
                old_v = dv.get('old', '?') if isinstance(dv, dict) else '?'
                new_v = dv.get('new', '?') if isinstance(dv, dict) else str(dv)
                body_html += '<div class="beautify-delta-item"><span class="beautify-delta-key">' + _html_escape(dk) + '</span> <span class="beautify-delta-old">' + _html_escape(str(old_v)) + '</span> → <span class="beautify-delta-new">' + _html_escape(str(new_v)) + '</span></div>'
            body_html += '</div>'

        body_html += '</div>'  # end tab-panel

    # ---- Assemble full panel ----
    panel_html = ''

    # Font loading
    if font_css:
        panel_html += '<style>' + font_css + '</style>'

    panel_html += '<div class="beautify-panel-inline" style="' + bg_style + '">'

    # Overlay for readability when bg is set
    if panel_bg:
        panel_html += '<div class="beautify-panel-overlay">'

    panel_html += '<div class="beautify-dashboard">'

    # Header with user avatar
    panel_html += '<div class="beautify-header">'
    if user_avatar:
        panel_html += '<div class="beautify-user-avatar" style="background-image:url(' + _html_escape(user_avatar) + ')"></div>'
    panel_html += '<div class="beautify-header-text">'
    panel_html += '<span class="beautify-header-title">' + _html_escape(panel_title or '状态面板') + '</span>'
    if user_name:
        panel_html += '<span class="beautify-header-sub">' + _html_escape(user_name) + '</span>'
    panel_html += '</div></div>'

    # Tabs
    panel_html += '<div class="beautify-tabs">' + tabs_html + '</div>'

    # Tab body
    panel_html += '<div class="beautify-tab-content">' + body_html + '</div>'

    panel_html += '</div>'  # end dashboard

    if panel_bg:
        panel_html += '</div>'  # end overlay

    panel_html += '</div>'  # end panel-inline

    # Font family
    if panel_font:
        panel_html += '<style>.beautify-panel-inline .beautify-dashboard{font-family:"' + _html_escape(panel_font) + '",sans-serif;}</style>'

    # Tab switching script
    panel_html += '''<script>
(function(){
  var panel = document.querySelector('.beautify-panel-inline');
  if (!panel || panel.getAttribute('data-tab-wired')) return;
  panel.setAttribute('data-tab-wired', '1');
  var tabs = panel.querySelectorAll('.beautify-tab-btn');
  var panels = panel.querySelectorAll('.beautify-tab-panel');
  for (var i = 0; i < panels.length; i++) {
    panels[i].style.display = (i === 0) ? '' : 'none';
  }
  for (var j = 0; j < tabs.length; j++) {
    tabs[j].addEventListener('click', function(e) {
      var tabId = this.getAttribute('data-tab');
      for (var k = 0; k < tabs.length; k++) {
        tabs[k].classList.remove('active');
      }
      this.classList.add('active');
      for (var m = 0; m < panels.length; m++) {
        panels[m].style.display = (panels[m].getAttribute('data-tab') === tabId) ? '' : 'none';
      }
    });
  }
})();
</script>'''

    return panel_html


def _escape_attr(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_tags(text, tag):
    return re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL).strip()


def _strip_mvu_commands(text):
    """Strip MVU _.set/add/insert etc. commands and UpdateVariable/json_patch blocks.

    These are the MVU engine's responsibilities — the card author's regex
    scripts handle <UpdateVariable> blocks for ST compatibility, but bare
    _.set() lines must be removed by us before the content reaches the user.
    """
    # Bare lodash-style commands: _.set('path', value);
    text = re.sub(
        r"^\s*_\.(?:set|insert|assign|remove|unset|delete|add|move)\s*\(.*?\)\s*;?\s*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    # <json_patch> blocks
    text = re.sub(
        r"<json_patch>[\s\S]*?</json_patch>",
        "",
        text,
    )
    # <UpdateVariable> blocks
    text = re.sub(
        r"<UpdateVariable>[\s\S]*?</UpdateVariable>",
        "",
        text,
    )
    # Collapse consecutive blank lines
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _text_to_p(text):
    """Convert plain text with \\r\\n\\r\\n paragraph breaks to <p>-wrapped HTML."""
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Split on double newlines (blank lines between paragraphs)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{p}</p>" for p in paras)


def _extract_options(ai_text):
    """Extract options block from AI text, preserving original."""
    m = re.search(r"<options>(.*?)</options>", ai_text, re.DOTALL)
    return m.group(1).strip() if m else ""
