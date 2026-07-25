from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class QualityVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    metrics: dict | None = None


@dataclass(frozen=True)
class QualityPolicy:
    """Minimal library-side length policy for committed narrative content.

    The policy is intentionally generic: it does not copy gameplay delivery
    thresholds. If the session snapshot exposes ``settings.wordCount``, we derive
    a visible-text character band around that target. Otherwise we fall back to a
    permissive default band so existing non-gameplay tests and library callers do
    not unexpectedly fail.
    """

    min_chars: int = 1
    max_chars: int = 20000
    min_ratio: float = 0.35
    max_ratio: float = 3.0
    min_floor: int = 1
    max_ceiling: int = 20000

    def resolve(self, settings: dict | None) -> tuple[int, int]:
        target = None
        if isinstance(settings, dict):
            raw = settings.get("wordCount")
            if isinstance(raw, (int, float)) and raw > 0:
                target = int(raw)
        if target is None:
            return self.min_chars, self.max_chars
        minimum = max(self.min_floor, int(target * self.min_ratio))
        maximum = min(self.max_ceiling, max(minimum, int(target * self.max_ratio)))
        return minimum, maximum


@dataclass(frozen=True)
class QualityContext:
    settings: dict = field(default_factory=dict)
    task_id: str | None = None
    base_revision: int | None = None


class QualityGate:
    def validate(self, draft, context: QualityContext) -> QualityVerdict:
        raise NotImplementedError


class DefaultQualityGate(QualityGate):
    def __init__(self, policy: QualityPolicy | None = None) -> None:
        self._policy = policy or QualityPolicy()

    def validate(self, draft, context: QualityContext) -> QualityVerdict:
        visible = visible_text_length(draft.content)
        minimum, maximum = self._policy.resolve(context.settings)
        reasons: list[str] = []
        if visible < minimum:
            reasons.append("content_too_short")
        if visible > maximum:
            reasons.append("content_too_long")
        return QualityVerdict(
            ok=not reasons,
            reasons=tuple(reasons),
            metrics={
                "visible_chars": visible,
                "min_chars": minimum,
                "max_chars": maximum,
            },
        )


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def visible_text_length(text: str) -> int:
    stripped = _TAG_RE.sub("", text or "")
    collapsed = _WHITESPACE_RE.sub("", stripped)
    return len(collapsed)
