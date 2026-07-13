from __future__ import annotations

import re

RETRIEVAL_ROUTING_RULE = (
    "Before defining SQL for rankings, time periods, customer behavior, returns, "
    "comparisons, or follow-up cohorts, you must call retrieve_golden_examples with "
    "a standalone, context-resolved question. Apply relevant precedent when defining "
    "SQL, but never treat its historical rows or narrative as current data. Retrieval "
    "is optional only for schema explanations, clarification requests, unsupported "
    "requests, and simple queries whose metric and scope are already unambiguous."
)

_PRECEDENT_REQUIRED = re.compile(
    r"\b(?:"
    r"bottom|churn|cohorts?|compare|comparison|customer\s+behavior|difference|"
    r"highest|least|lowest|more|most|"
    r"monthly|(?:month|quarter|week|day|year)[ -]to[ -]date|"
    r"(?:prior|previous|current|this|past|last)\s+(?:\d+\s+)?"
    r"(?:days?|weeks?|months?|quarters?|years?)|quarterly|"
    r"rank(?:ed|ing|s)?|refunds?|repeat\s+customers?|retention|returns?|spend|"
    r"time\s+(?:period|window)|today|top|trends?|versus|weekly|yearly|yesterday|ytd"
    r")\b",
    re.IGNORECASE,
)
_FOLLOW_UP_REFERENCE = re.compile(
    r"\b(?:prior|previous|same|that|them|those|what about|which one)\b",
    re.IGNORECASE,
)


def requires_golden_precedent(question: str, *, has_history: bool) -> bool:
    """Classify questions whose SQL must follow an approved analytical precedent."""

    return bool(
        _PRECEDENT_REQUIRED.search(question)
        or (has_history and _FOLLOW_UP_REFERENCE.search(question))
    )
