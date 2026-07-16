from __future__ import annotations

import re

RETRIEVAL_ROUTING_RULE = (
    "Choose retrieve_golden_examples before SQL when approved precedent would help "
    "resolve a metric, cohort, join, filter, ranking, time window, comparison, return, "
    "customer-behavior, or follow-up definition. Pass a standalone, context-resolved "
    "question and apply relevant precedent when defining SQL, but never treat historical "
    "rows or narrative as current data. Skip retrieval when it would not improve the "
    "analysis, including schema explanations and simple unambiguous queries."
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


def is_schema_question(question: str) -> bool:
    """Identify high-confidence schema introspection that must not use data tools."""

    return bool(_SCHEMA_QUESTION.search(question))
