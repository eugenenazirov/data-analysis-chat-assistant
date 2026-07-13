from __future__ import annotations

from typing import Any, Protocol


class Telemetry(Protocol):
    def event(self, trace_id: str, name: str, **fields: Any) -> None: ...
