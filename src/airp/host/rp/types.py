"""Small value objects shared by the RP host transport and runtime.

Keeping these records independent from the SQLite implementation prevents
commands, compatibility readers, and projection adapters from importing the
large session runtime just to describe a result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnDraft:
    """Structured narrative turn produced by the active Graph."""

    content: str
    summary: str = ""
    options: str = ""
    polished_input: str = ""
    mvu_commands: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "content": self.content,
                "summary": self.summary,
                "options": self.options,
                "polished_input": self.polished_input,
                "mvu_commands": self.mvu_commands,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "TurnDraft":
        data = json.loads(raw)
        return cls(
            content=data["content"],
            summary=data.get("summary", ""),
            options=data.get("options", ""),
            polished_input=data.get("polished_input", ""),
            mvu_commands=data.get("mvu_commands", ""),
        )


@dataclass(frozen=True)
class RuntimeResult:
    task_id: str
    commit_id: str | None
    revision: int
    status: str
    attempt: int = 0


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    type: str
    payload: dict


@dataclass(frozen=True)
class TurnCommit:
    id: str
    revision: int
    task_id: str
