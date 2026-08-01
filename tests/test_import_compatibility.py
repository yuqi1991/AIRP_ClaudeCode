"""Representative SillyTavern card import compatibility contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.import_card import run_import  # noqa: E402
from airp.engine.render import resolve_card_macros  # noqa: E402


def test_sillytavern_identity_macros_resolve_for_display():
    text = "{{char}}在璃月港看向{{user}}，{{USER}}点了点头。"

    assert resolve_card_macros(
        text, user_name="旅行者", character_name="刻晴"
    ) == "刻晴在璃月港看向旅行者，旅行者点了点头。"


def test_structured_worldbook_profile_is_not_guessed_as_runtime_state(tmp_path):
    root = tmp_path / "root"
    styles = root / "styles"
    styles.mkdir(parents=True)
    card = tmp_path / "card"
    card.mkdir()
    payload = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "刻晴",
            "description": "璃月七星中的玉衡星。",
            "first_mes": "刻晴在璃月港向{{user}}走来。",
            "alternate_greetings": [],
            "character_book": {
                "entries": [
                    {
                        "comment": "芙宁娜",
                        "keys": ["芙宁娜"],
                        "content": (
                            "name: 芙宁娜\n"
                            "race: 魔神\n"
                            "age: 超过500岁\n"
                            "personality:\n"
                            "  - 浮夸: 喜欢表演"
                        ),
                    }
                ]
            },
        },
    }
    (card / "刻晴.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    result = run_import(str(card), str(root))

    assert result["status"] == "ok"
    assert result["source_type"] == "json"
    assert result["worldbook_entries_total"] == 1
    assert result["initvar_keys"] == []
    assert result["initvar_source"] == ""
    assert not (card / ".initvar.json").exists()
    assert (card / ".session_init").is_file()
    openings = json.loads((card / "memory" / "openings.json").read_text(encoding="utf-8"))
    assert "variables" not in openings[0]
