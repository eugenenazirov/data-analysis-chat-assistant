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
_SCHEMA_QUESTION = re.compile(
    r"\b(?:"
    r"schema|database structure|"
    r"(?:what|which)(?: safe)?(?: retail)? tables|"
    r"(?:what|which|list|show|describe|explain)(?: are)?(?: the)? "
    r"(?:available|safe)(?: retail)? tables|"
    r"(?:what|which)(?: safe)? columns|available columns|"
    r"what fields|which fields|"
    r"data (?:can|could) you (?:access|analy[sz]e)"
    r")\b",
    re.IGNORECASE,
)


def requires_golden_precedent(question: str, *, has_history: bool) -> bool:
    """Classify questions whose SQL must follow an approved analytical precedent."""

    return bool(
        _PRECEDENT_REQUIRED.search(question)
        or (has_history and _FOLLOW_UP_REFERENCE.search(question))
    )


def is_schema_question(question: str) -> bool:
    """Identify high-confidence schema introspection that must not use data tools."""

    return bool(_SCHEMA_QUESTION.search(question))
