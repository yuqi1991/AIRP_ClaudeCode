"""User-owned JavaScript regular-expression transformations.

The engine deliberately keeps this module independent from Studio persistence,
Projects and Graph Runtime.  Callers provide an ordered collection of rule
objects and choose whether the current value is an ``input`` or ``output``.
JavaScript owns matching and replacement semantics; Python only coordinates the
request and turns diagnostics/errors into a stable interface.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Any


_WORKER = r"""
const fs = require('node:fs');

function fail(error) {
  process.stdout.write(JSON.stringify({ok: false, error}));
}

try {
  const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
  const target = payload.target;
  let value = String(payload.text);
  const diagnostics = [];
  const maxOutputBytes = Number(payload.max_output_bytes || 0);

  for (const rawRule of (payload.rules || [])) {
    const rule = rawRule || {};
    const ruleId = String(rule.id || rule.rule_id || 'rule');
    const name = String(rule.name || ruleId);
    const ruleTarget = String(rule.target || 'output');
    const enabled = rule.enabled !== false;
    const applies = enabled && (ruleTarget === 'both' || ruleTarget === target);

    if (!enabled || !applies) {
      diagnostics.push({
        rule_id: ruleId,
        name,
        target: ruleTarget,
        applied: false,
        skipped: true,
        matched: false,
        changed: false,
        match_count: 0,
      });
      continue;
    }

    const pattern = String(rule.pattern ?? '');
    const flags = String(rule.flags ?? '');
    const replacement = String(rule.replacement ?? '');
    let expression;
    try {
      expression = new RegExp(pattern, flags);
    } catch (error) {
      fail({
        code: 'invalid_regex',
        message: String(error && error.message || error),
        rule_id: ruleId,
        name,
        target,
      });
      process.exit(0);
    }

    // Matching is observed with a fresh expression so replacement never
    // inherits a stateful lastIndex from global/sticky expressions.
    let matches = null;
    try {
      matches = value.match(new RegExp(pattern, flags));
    } catch (error) {
      fail({
        code: 'invalid_regex',
        message: String(error && error.message || error),
        rule_id: ruleId,
        name,
        target,
      });
      process.exit(0);
    }
    const before = value;
    value = value.replace(expression, replacement);
    if (maxOutputBytes > 0 && Buffer.byteLength(value, 'utf8') > maxOutputBytes) {
      fail({
        code: 'regex_output_too_large',
        message: `transformation output exceeds ${maxOutputBytes} bytes`,
        rule_id: ruleId,
        name,
        target,
      });
      process.exit(0);
    }
    diagnostics.push({
      rule_id: ruleId,
      name,
      target: ruleTarget,
      applied: true,
      skipped: false,
      matched: matches !== null,
      changed: before !== value,
      match_count: matches === null ? 0 : (flags.includes('g') ? matches.length : 1),
    });
  }

  process.stdout.write(JSON.stringify({ok: true, text: value, diagnostics}));
} catch (error) {
  fail({
    code: 'regex_worker_failed',
    message: String(error && error.message || error),
  });
}
"""


@dataclass(frozen=True)
class RegexRuleDiagnostic:
    """Observable outcome for one configured rule."""

    rule_id: str
    name: str
    target: str
    applied: bool
    skipped: bool
    matched: bool
    changed: bool
    match_count: int = 0

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegexRuleDiagnostic":
        return cls(
            rule_id=str(payload.get("rule_id") or "rule"),
            name=str(payload.get("name") or payload.get("rule_id") or "rule"),
            target=str(payload.get("target") or "output"),
            applied=bool(payload.get("applied", False)),
            skipped=bool(payload.get("skipped", False)),
            matched=bool(payload.get("matched", False)),
            changed=bool(payload.get("changed", False)),
            match_count=int(payload.get("match_count") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "target": self.target,
            "applied": self.applied,
            "skipped": self.skipped,
            "matched": self.matched,
            "changed": self.changed,
            "match_count": self.match_count,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow trace/UI code to inspect diagnostics like a JSON record."""

        return self.to_dict()[key]


@dataclass(frozen=True)
class RegexTransformResult:
    """Text after ordered rule application and its per-rule outcomes."""

    text: str
    diagnostics: tuple[RegexRuleDiagnostic, ...]

    @property
    def transformed(self) -> str:
        """Readable alias used by trace consumers."""

        return self.text

    @property
    def transformed_text(self) -> str:
        return self.text

    @property
    def rules(self) -> tuple[RegexRuleDiagnostic, ...]:
        return self.diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "transformed": self.text,
            "text": self.text,
            "rules": [item.to_dict() for item in self.diagnostics],
        }


class RegexTransformError(RuntimeError):
    """A deterministic failure in a user-authored regex transformation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "regex_transform_failed",
        rule_id: str | None = None,
        rule_name: str | None = None,
        target: str | None = None,
    ) -> None:
        self.code = code
        self.rule_id = rule_id
        self.rule_name = rule_name
        self.target = target
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.rule_id is not None:
            payload["rule_id"] = self.rule_id
        if self.rule_name is not None:
            payload["rule_name"] = self.rule_name
        if self.target is not None:
            payload["target"] = self.target
        return payload


class RegexTransformer:
    """Apply ordered JavaScript regular-expression replacement rules.

    ``rules`` is optional at construction so a long-lived caller can reuse the
    module with per-run frozen collections.  ``transform`` accepts rules as its
    second positional argument as well, making the public seam explicit and
    convenient for collection test panels.
    """

    def __init__(
        self,
        rules: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        *,
        node_path: str | None = None,
        timeout_seconds: float = 5.0,
        max_input_bytes: int = 2_000_000,
        max_output_bytes: int = 4_000_000,
    ) -> None:
        self.rules = tuple(
            self._normalize_rule(rule, index)
            for index, rule in enumerate(self._coerce_rules(rules))
        )
        self.node_path = node_path or shutil.which("node") or "node"
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_input_bytes = max(1, int(max_input_bytes))
        self.max_output_bytes = max(1, int(max_output_bytes))

    def transform(
        self,
        text: str,
        rules: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        target: str = "output",
    ) -> RegexTransformResult:
        if not isinstance(text, str):
            raise RegexTransformError("regex transformations require string text", code="invalid_text")
        if target not in {"input", "output"}:
            raise RegexTransformError(
                "regex transformation target must be input or output",
                code="invalid_target",
                target=target,
            )
        normalized = (
            tuple(
                self._normalize_rule(rule, index)
                for index, rule in enumerate(self._coerce_rules(rules))
            )
            if rules is not None
            else self.rules
        )
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_input_bytes:
            raise RegexTransformError(
                f"transformation input exceeds {self.max_input_bytes} bytes",
                code="regex_input_too_large",
                target=target,
            )

        request = {
            "text": text,
            "target": target,
            "rules": [dict(rule) for rule in normalized],
            "max_output_bytes": self.max_output_bytes,
        }
        try:
            completed = subprocess.run(
                [self.node_path, "-e", _WORKER],
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RegexTransformError(
                f"regex worker timed out after {self.timeout_seconds:g}s",
                code="regex_timeout",
                target=target,
            ) from exc
        except OSError as exc:
            raise RegexTransformError(
                f"regex worker unavailable: {exc}",
                code="regex_worker_unavailable",
                target=target,
            ) from exc

        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RegexTransformError(
                message or f"regex worker exited with status {completed.returncode}",
                code="regex_worker_failed",
                target=target,
            )
        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegexTransformError(
                "regex worker returned invalid JSON",
                code="regex_worker_failed",
                target=target,
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, Mapping) else None
            error = error if isinstance(error, Mapping) else {}
            rule_id = str(error.get("rule_id")) if error.get("rule_id") is not None else None
            rule_name = str(error.get("name")) if error.get("name") is not None else None
            message = str(error.get("message") or "regex transformation failed")
            context = f" for rule {rule_id}" if rule_id else ""
            raise RegexTransformError(
                f"{message}{context}",
                code=str(error.get("code") or "regex_transform_failed"),
                rule_id=rule_id,
                rule_name=rule_name,
                target=str(error.get("target") or target),
            )
        value = payload.get("text")
        if not isinstance(value, str):
            raise RegexTransformError(
                "regex worker returned a non-string result",
                code="invalid_result",
                target=target,
            )
        raw_diagnostics = payload.get("diagnostics")
        if not isinstance(raw_diagnostics, list):
            raise RegexTransformError(
                "regex worker returned invalid diagnostics",
                code="regex_worker_failed",
                target=target,
            )
        diagnostics = tuple(
            RegexRuleDiagnostic.from_dict(item)
            for item in raw_diagnostics
            if isinstance(item, Mapping)
        )
        return RegexTransformResult(value, diagnostics)

    def apply(
        self,
        text: str,
        rules: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        target: str = "output",
    ) -> RegexTransformResult:
        """Compatibility alias for callers that describe this as an apply step."""

        return self.transform(text, rules, target)

    @staticmethod
    def _coerce_rules(
        rules: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    ) -> Iterable[Mapping[str, Any]]:
        if rules is None:
            return ()
        if isinstance(rules, Mapping):
            nested = rules.get("rules")
            if nested is None:
                return ()
            if not isinstance(nested, (list, tuple)):
                raise RegexTransformError(
                    "regex collection rules must be an array",
                    code="invalid_collection",
                )
            return nested
        return rules

    @staticmethod
    def _normalize_rule(rule: Mapping[str, Any], index: int) -> dict[str, Any]:
        if not isinstance(rule, Mapping):
            raise RegexTransformError(
                f"regex rule {index} must be an object",
                code="invalid_rule",
            )
        rule_id = rule.get("id", rule.get("rule_id", f"rule-{index + 1}"))
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise RegexTransformError(
                f"regex rule {index} requires a non-empty id",
                code="invalid_rule",
            )
        target = rule.get("target", "output")
        if target not in {"input", "output", "both"}:
            raise RegexTransformError(
                f"regex rule {rule_id} has invalid target {target!r}",
                code="invalid_rule_target",
                rule_id=rule_id,
                target=str(target),
            )
        pattern = rule.get("pattern", "")
        if not isinstance(pattern, str):
            raise RegexTransformError(
                f"regex rule {rule_id} pattern must be a string",
                code="invalid_rule",
                rule_id=rule_id,
                target=str(target),
            )
        flags = rule.get("flags", "")
        replacement = rule.get("replacement", "")
        if not isinstance(flags, str) or not isinstance(replacement, str):
            raise RegexTransformError(
                f"regex rule {rule_id} flags and replacement must be strings",
                code="invalid_rule",
                rule_id=rule_id,
                target=str(target),
            )
        return {
            "id": rule_id.strip(),
            "name": str(rule.get("name") or rule_id).strip(),
            "enabled": rule.get("enabled", True) is not False,
            "target": target,
            "pattern": pattern,
            "flags": flags,
            "replacement": replacement,
        }


__all__ = [
    "RegexRuleDiagnostic",
    "RegexTransformError",
    "RegexTransformResult",
    "RegexTransformer",
]
