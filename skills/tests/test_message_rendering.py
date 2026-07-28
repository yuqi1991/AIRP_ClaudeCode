"""Regression coverage for message-bubble content preparation."""

import json
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from handler import write_content_js  # noqa: E402


def test_user_message_is_escaped_but_markdown_tokens_and_newlines_survive(tmp_path):
    card = tmp_path / "card"
    projection = tmp_path / "projection"
    card.mkdir()
    (card / "chat_log.json").write_text(
        json.dumps(
            [
                {
                    "index": 0,
                    "user": "**加粗**\n第二行 <not-a-tag>",
                    "ai": "<p>AI **加粗**\n下一行</p>",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    write_content_js(card, projection_root=projection)
    generated = (projection / "content.js").read_text(encoding="utf-8")

    assert "**加粗**\\n第二行 &lt;not-a-tag&gt;" in generated
    assert "<p>AI **加粗**\\n下一行</p>" in generated


def test_index_declares_message_formatting_for_bubbles():
    index = (SKILLS / "styles" / "index.html").read_text(encoding="utf-8")

    assert "white-space: pre-wrap" in index
    assert "formatMessageBubbles(contentEl)" in index
    assert "**bold**" in index
