"""engine.turn_parser — harness-owned narrative text → TurnDraft.

The model produces legacy ``response.txt``-shaped narrative text. Commit is a
**harness** action: this module deterministically parses that text into a
:class:`~engine.runtime.TurnDraft` so the runtime can validate and commit
without any model-facing commit tool.

Parse contract (never raises for missing tags — missing → sane defaults):

* ``<polished_input>…</polished_input>`` → ``polished_input`` (optional)
* ``<content>…</content>`` → ``content``; if absent, the whole text (minus
  known tags) is treated as content
* ``<summary>…</summary>`` → ``summary`` (default ``""``)
* ``<options>…</options>`` → ``options`` (default ``""``)
* MVU: prefer a standalone ``<UpdateVariable>…</UpdateVariable>`` block as
  ``mvu_commands``; otherwise leave empty so commit-time
  ``extract_commands(content)`` still finds inline ``_.set`` / JSONPatch
  inside content (same dual-source path as ``_projected_state_from_base``)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from airp.engine.runtime import TurnDraft


_TAG_RE = {
    "polished_input": re.compile(
        r"<polished_input>(.*?)</polished_input>", re.DOTALL | re.IGNORECASE
    ),
    "content": re.compile(r"<content>(.*?)</content>", re.DOTALL | re.IGNORECASE),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE),
    "options": re.compile(r"<options>(.*?)</options>", re.DOTALL | re.IGNORECASE),
    "update_variable": re.compile(
        r"<UpdateVariable\b[^>]*>.*?</UpdateVariable>", re.DOTALL | re.IGNORECASE
    ),
    # Strip tokens block if present (harness may re-attach later); not part of TurnDraft.
    "tokens": re.compile(r"<tokens\b[^>]*>.*?</tokens>", re.DOTALL | re.IGNORECASE),
}


def _first(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text or "")
    if not match:
        return ""
    if match.lastindex:
        return match.group(1).strip()
    return match.group(0).strip()


def _strip_known_tags(text: str) -> str:
    stripped = text or ""
    for key in ("polished_input", "content", "summary", "options", "update_variable", "tokens"):
        stripped = _TAG_RE[key].sub("", stripped)
    return stripped.strip()


def parse_turn_text(text: str, *, fallback_input: str = "") -> "TurnDraft":
    """Parse model narrative text into a :class:`TurnDraft`.

    Never raises for missing/malformed tags. Empty or whitespace-only input
    yields an empty-content draft (quality gate decides whether that is
    acceptable — the harness flow itself always completes).
    """
    # Lazy import avoids circular import with engine.runtime (which owns TurnDraft
    # and calls this parser from the harness commit path).
    from airp.engine.runtime import TurnDraft

    raw = text if isinstance(text, str) else ""
    polished = _first(_TAG_RE["polished_input"], raw)
    content = _first(_TAG_RE["content"], raw)
    summary = _first(_TAG_RE["summary"], raw)
    options = _first(_TAG_RE["options"], raw)
    # Capture the full UpdateVariable block (including tags) so extract_commands
    # can see JSONPatch / _.set inside it — same source shape the live pipeline uses.
    mvu_match = _TAG_RE["update_variable"].search(raw)
    mvu_commands = mvu_match.group(0).strip() if mvu_match else ""

    if not content:
        # No <content> tag: treat residual prose (minus other known tags) as content.
        residual = _strip_known_tags(raw)
        content = residual

    if not polished and fallback_input:
        polished = fallback_input

    return TurnDraft(
        content=content or "",
        summary=summary or "",
        options=options or "",
        polished_input=polished or "",
        mvu_commands=mvu_commands or "",
    )


def format_turn_text(
    content: str,
    *,
    summary: str = "",
    options: str = "",
    polished_input: str = "",
    mvu_commands: str = "",
) -> str:
    """Inverse helper for tests: build a legacy-tagged narrative blob from parts."""
    parts: list[str] = []
    if polished_input:
        parts.append(f"<polished_input>{polished_input}</polished_input>")
    parts.append(f"<content>\n{content}\n</content>")
    if mvu_commands:
        # Accept either a bare _.set / JSONPatch payload or a full UpdateVariable block.
        block = mvu_commands.strip()
        if not block.lower().startswith("<updatevariable"):
            block = f"<UpdateVariable>\n{block}\n</UpdateVariable>"
        parts.append(block)
    if summary:
        parts.append(f"<summary>{summary}</summary>")
    if options:
        parts.append(f"<options>\n{options}\n</options>")
    return "\n".join(parts)
