from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_KEY_RE = re.compile(
    r'(authorization|token|password|passwd|secret|cookie|set-cookie|sendkey|phone)',
    re.IGNORECASE,
)
REDACTED = '<redacted>'


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, set):
        return {redact_sensitive(item) for item in value}
    return value


def redact_text(value: Any, *, max_length: int = 500) -> str:
    text = str(value or '')
    text = re.sub(r'(?i)(authorization|token|password|passwd|secret|cookie|sendkey)=(?:Bearer\s+)?\S+', r'\1=<redacted>', text)
    text = re.sub(r'Bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer <redacted>', text, flags=re.IGNORECASE)
    if len(text) > max_length:
        text = text[:max_length] + '...'
    return text
