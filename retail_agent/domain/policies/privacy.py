from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3}[\s.-]?\d{4}(?!\w)"
)


def redact_text(value: str) -> tuple[str, int]:
    redactions = 0

    def _email(_: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return "[REDACTED_EMAIL]"

    def _phone(_: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return "[REDACTED_PHONE]"

    value = EMAIL_RE.sub(_email, value)
    value = PHONE_RE.sub(_phone, value)
    return value, redactions


def redact_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        redacted = []
        count = 0
        for item in value:
            new_item, item_count = redact_value(item)
            redacted.append(new_item)
            count += item_count
        return redacted, count
    if isinstance(value, tuple):
        redacted = []
        count = 0
        for item in value:
            new_item, item_count = redact_value(item)
            redacted.append(new_item)
            count += item_count
        return tuple(redacted), count
    if isinstance(value, dict):
        redacted_dict = {}
        count = 0
        for key, item in value.items():
            new_item, item_count = redact_value(item)
            redacted_dict[key] = new_item
            count += item_count
        return redacted_dict, count
    return value, 0
