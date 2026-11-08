"""Sensitive-data redaction for logs, events and audit exports."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|session|credential|private[_-]?key|iban|account_number)",
    re.IGNORECASE,
)

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b"), "[REDACTED_IBAN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_NUMBER]"),
    (re.compile(r"(?i)(password|secret|token|api_key)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
]


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    out = text
    for secret in extra_secrets or []:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


# Numeric usage counters whose names happen to contain "token"; they are not credentials.
NON_SENSITIVE_KEYS = frozenset({"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"})


def redact(value: Any, extra_secrets: list[str] | None = None) -> Any:
    """Recursively redact dict/list/str values. Keys that look sensitive are masked entirely."""
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if isinstance(k, str) and k in NON_SENSITIVE_KEYS and isinstance(v, (int, float)):
                result[k] = v
            elif isinstance(k, str) and SENSITIVE_KEYS.search(k) and not isinstance(v, (dict, list)):
                result[k] = "[REDACTED]"
            else:
                result[k] = redact(v, extra_secrets)
        return result
    if isinstance(value, list):
        return [redact(v, extra_secrets) for v in value]
    if isinstance(value, str):
        return redact_text(value, extra_secrets)
    return value
