"""Regression coverage for message-bubble content preparation."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.handler import write_content_js  # noqa: E402


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
    index = (REPO_ROOT / "src" / "airp" / "web" / "index.html").read_text(encoding="utf-8")

    assert "white-space: pre-wrap" in index
    assert "formatMessageBubbles(contentEl)" in index
    assert "renderInlineMarkdown" in index
    assert "本回合提交了空正文" in index
    assert "bubble.dataset.airpEmptyOutput = 'true'" in index


def test_index_supports_full_markdown_subset_without_card_script_execution():
    index = (REPO_ROOT / "src" / "airp" / "web" / "index.html").read_text(encoding="utf-8")

    for marker in (
        "renderInlineMarkdown",
        "createElement('h' + heading[1].length)",
        "createElement('blockquote')",
        "createElement(ordered ? 'ol' : 'ul')",
        "createElement('pre')",
        "createElement('hr')",
        "createElement('del')",
        "safeMarkupUrl",
        "safeMarkupStyle",
        "replaceElementContents",
        "airp-markdown-table-wrap",
    ):
        assert marker in index

    # The browser must never promote card-provided script nodes into executable
    # nodes. Dynamic state/content.js loading is intentionally separate and is
    # not part of the card/AI markup sink.
    assert "Re-execute embedded" not in index
    assert "oldS.parentNode.replaceChild" not in index
    assert "oldSB.parentNode.replaceChild" not in index
    assert "const oldS =" not in index
    assert "const oldSB =" not in index
