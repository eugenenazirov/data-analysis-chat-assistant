"""Compatibility exports for infrastructure telemetry."""

from retail_agent.infrastructure.observability import (
    EventLogger,
    maybe_configure_logfire,
    new_trace_id,
)

__all__ = ["EventLogger", "maybe_configure_logfire", "new_trace_id"]
