from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NonQueryDisposition:
    """A high-confidence request outcome that does not require data tools."""

    kind: Literal["clarify", "refuse"]
    answer: str

    @property
    def refused(self) -> bool:
        return self.kind == "refuse"


_CUSTOMER_RANKING = re.compile(r"\bbest\s+customers?\b", re.IGNORECASE)
_CUSTOMER_METRIC = re.compile(
    r"\b(?:average\s+order\s+value|frequency|lifetime|margin|orders?|revenue|spend)\b",
    re.IGNORECASE,
)
_CONFLICTING_SCOPE = re.compile(
    r"(?:\ball\b.*\b(?:top|bottom)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b)"
    r"|(?:\b(?:top|bottom)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b.*\ball\b)",
    re.IGNORECASE,
)
_VISITOR_CONVERSION = re.compile(
    r"\b(?:visitor|visit|session|impression)s?[- ]to[- ](?:order|purchase)\b|"
    r"\b(?:visitor|visit|session|impression)s?\b.*\bconversion\s+rate\b",
    re.IGNORECASE,
)
_PHYSICAL_BRANCH = re.compile(
    r"\bphysical\s+(?:store\s+)?(?:branches|branch|locations|location)\b|"
    r"\bstore\s+branches?\b",
    re.IGNORECASE,
)


def classify_non_query_request(question: str) -> NonQueryDisposition | None:
    """Route only unambiguous limitations before model or warehouse work."""

    if _VISITOR_CONVERSION.search(question):
        return NonQueryDisposition(
            kind="refuse",
            answer=(
                "Visitor-to-order conversion cannot be calculated from the approved retail "
                "tables because they contain users and orders, but no visit, session, or "
                "impression denominator. I can compare realized orders or revenue by traffic "
                "source instead."
            ),
        )
    if _PHYSICAL_BRANCH.search(question):
        return NonQueryDisposition(
            kind="clarify",
            answer=(
                "The approved schema has no physical store-branch dimension, and I will not "
                "silently substitute customer geography as a branch proxy. Would you like a "
                "customer-state comparison instead?"
            ),
        )
    if _CONFLICTING_SCOPE.search(question):
        return NonQueryDisposition(
            kind="clarify",
            answer=(
                "Should the result include every matching customer or only the requested "
                "ranked subset? Please choose one scope before I query the warehouse."
            ),
        )
    if _CUSTOMER_RANKING.search(question) and not _CUSTOMER_METRIC.search(question):
        return NonQueryDisposition(
            kind="clarify",
            answer=(
                "What should define a best customer: realized spend, completed-order count, "
                "purchase frequency, or another metric, and for which time period?"
            ),
        )
    return None
