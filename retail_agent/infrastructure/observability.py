from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from retail_agent.domain.policies.privacy import redact_value


def new_trace_id() -> str:
    return uuid.uuid4().hex


class EventLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, trace_id: str, name: str, **fields: Any) -> None:
        redacted_fields, redaction_count = redact_value(fields)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trace_id": trace_id,
            "event": name,
            "redactions": redaction_count,
            **redacted_fields,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def maybe_configure_logfire(enable: bool) -> None:
    if not enable:
        return
    try:
        import logfire

        logfire.configure()
        logfire.instrument_pydantic_ai()
    except Exception:
        return
