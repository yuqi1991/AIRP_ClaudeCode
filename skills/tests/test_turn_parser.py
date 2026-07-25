"""Harness text → TurnDraft parse contract (commit is harness-owned)."""

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.turn_parser import format_turn_text, parse_turn_text


def test_parse_full_tagged_blob_extracts_all_fields():
    text = format_turn_text(
        content="<p>海风掠过礁石。</p>",
        summary="玩家来到海边",
        options='<font color="#5a7a5a">继续观察</font>',
        polished_input="我走向礁石",
        mvu_commands="_.set('世界.时间', '1月1日 10:00');",
    )
    draft = parse_turn_text(text)
    assert draft.content == "<p>海风掠过礁石。</p>"
    assert draft.summary == "玩家来到海边"
    assert "继续观察" in draft.options
    assert draft.polished_input == "我走向礁石"
    assert "UpdateVariable" in draft.mvu_commands
    assert "世界.时间" in draft.mvu_commands


def test_parse_missing_tags_uses_sane_defaults_never_raises():
    draft = parse_turn_text("只是一段没有标签的散文。海边的风。")
    assert draft.content == "只是一段没有标签的散文。海边的风。"
    assert draft.summary == ""
    assert draft.options == ""
    assert draft.mvu_commands == ""
    assert draft.polished_input == ""


def test_parse_empty_text_yields_empty_content_not_error():
    draft = parse_turn_text("")
    assert draft.content == ""
    assert draft.summary == ""
    draft2 = parse_turn_text("   \n  ")
    assert draft2.content == ""


def test_parse_fallback_input_fills_polished_when_tag_absent():
    draft = parse_turn_text(
        "<content><p>正文</p></content><summary>摘</summary>",
        fallback_input="原始输入",
    )
    assert draft.polished_input == "原始输入"
    assert draft.content == "<p>正文</p>"
    assert draft.summary == "摘"


def test_parse_inline_updatevariable_block_becomes_mvu_commands():
    text = """
<content><p>浪头扑上来。</p></content>
<UpdateVariable>
<JSONPatch>
[{"op": "replace", "path": "/世界/时间", "value": "1月1日 11:00"}]
</JSONPatch>
</UpdateVariable>
<summary>涨潮</summary>
"""
    draft = parse_turn_text(text)
    assert "浪头扑上来" in draft.content
    assert "<UpdateVariable>" in draft.mvu_commands
    assert "/世界/时间" in draft.mvu_commands
    assert draft.summary == "涨潮"


def test_parse_options_multiline_preserved():
    text = """
<content><p>x</p></content>
<summary>s</summary>
<options>
<font color="#5a7a5a">😏 选项一</font>
<font color="#b06a3d">😈 选项二</font>
</options>
"""
    draft = parse_turn_text(text)
    assert "选项一" in draft.options
    assert "选项二" in draft.options
