from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from autodl_helper.security import redact_sensitive, redact_text


def json_error(code: str, message: str, *, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        'code': code,
        'message': redact_text(message),
    }
    if details is not None:
        error['details'] = redact_sensitive(details)
    return {'ok': False, 'error': error}


def json_ok(data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {'ok': True}
    if data is not None:
        payload['data'] = data
    return payload


def print_json(payload: Any, *, output: TextIO | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=output or sys.stdout)


def print_json_error(code: str, message: str, *, details: Any = None, output: TextIO | None = None) -> None:
    print_json(json_error(code, message, details=details), output=output or sys.stderr)
