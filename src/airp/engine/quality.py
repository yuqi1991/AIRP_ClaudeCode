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
    """Technical gate for a commit-capable narrative payload.

    User-authored writing preferences such as target length, style, person,
    summaries and options belong in editable Agent instructions. The runtime only
    rejects an empty visible payload here; upper/lower length bounds are retained
    as explicit opt-in constructor knobs for tests and embedders.
    """

    min_chars: int = 1
    max_chars: int | None = None

    def bounds(self) -> tuple[int, int | None]:
        return self.min_chars, self.max_chars


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
        minimum, maximum = self._policy.bounds()
        reasons: list[str] = []
        if visible < minimum:
            reasons.append("content_too_short")
        if maximum is not None and visible > maximum:
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
