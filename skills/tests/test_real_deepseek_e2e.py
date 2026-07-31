"""Opt-in DeepSeek smoke test through the canonical HTTP Provider Adapter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.provider import (  # noqa: E402
    AbortSignal,
    OpenAICompatibleProviderAdapter,
    ProviderDelta,
    ProviderRequest,
    ProviderResult,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"),
    reason="DEEPSEEK_API_KEY not set; real DeepSeek E2E is opt-in",
)


def test_real_deepseek_streams_through_canonical_provider_adapter():
    adapter = OpenAICompatibleProviderAdapter(
        base_url="https://api.deepseek.com",
        api_key=os.environ["DEEPSEEK_API_KEY"],
        api_format="chat_completions",
        model="deepseek-v4-flash",
    )
    items = list(
        adapter.stream(
            ProviderRequest(
                messages=[{"role": "user", "content": "请用两句中文描写清晨的海边。"}],
                tools=[],
                model="deepseek-v4-flash",
                metadata={"test": "real-deepseek"},
            ),
            AbortSignal(),
        )
    )
    deltas = [item for item in items if isinstance(item, ProviderDelta)]
    results = [item for item in items if isinstance(item, ProviderResult)]
    text = "".join(item.text or "" for item in deltas)
    assert text.strip()
    assert any("一" <= char <= "鿿" for char in text)
    assert results and results[-1].usage.total_tokens > 0
