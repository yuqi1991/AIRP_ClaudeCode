"""Parse historic AIRP tagged chat entries during one-time card import only."""

from __future__ import annotations

import re


_TAG_RE = {
    "polished_input": re.compile(r"<polished_input>(.*?)</polished_input>", re.DOTALL | re.IGNORECASE),
    "content": re.compile(r"<content>(.*?)</content>", re.DOTALL | re.IGNORECASE),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE),
    "options": re.compile(r"<options>(.*?)</options>", re.DOTALL | re.IGNORECASE),
    "update_variable": re.compile(r"<UpdateVariable\b[^>]*>.*?</UpdateVariable>", re.DOTALL | re.IGNORECASE),
    "tokens": re.compile(r"<tokens\b[^>]*>.*?</tokens>", re.DOTALL | re.IGNORECASE),
}


def parse_legacy_turn(text: str, *, fallback_input: str = ""):
    """Normalize an old tagged chat entry before storing its durable history."""
    from airp.host.rp.session_runtime import TurnDraft

    raw = text if isinstance(text, str) else ""

    def first(key: str) -> str:
        match = _TAG_RE[key].search(raw)
        if not match:
            return ""
        return (match.group(1) if match.lastindex else match.group(0)).strip()

    content = first("content")
    if not content:
        content = raw
        for pattern in _TAG_RE.values():
            content = pattern.sub("", content)
        content = content.strip()
    variable = _TAG_RE["update_variable"].search(raw)
    return TurnDraft(
        content=content,
        summary=first("summary"),
        options=first("options"),
        polished_input=first("polished_input") or fallback_input,
        mvu_commands=variable.group(0).strip() if variable else "",
    )
