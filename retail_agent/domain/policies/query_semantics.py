from __future__ import annotations

import re
from datetime import date

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from retail_agent.sql_guard import normalized_table_aliases


class QuerySemanticError(ValueError):
    """Raised when valid SQL contradicts an explicit question constraint."""


_RELATIVE_PERIOD = re.compile(
    r"\b(?:"
    r"(?:last|previous|prior|past)\s+"
    r"(?:complete(?:d)?\s+|fully\s+completed\s+)?(?:calendar\s+)?"
    r"(?:day|week|month|quarter|year|\d+\s+(?:days|weeks|months|quarters|years))"
    r"|(?:two|three|four|\d+)\s+most\s+recent\s+"
    r"(?:complete(?:d)?\s+)?(?:days|weeks|months|quarters|years)"
    r"|trailing\s+\d+\s+(?:days|weeks|months|quarters|years)"
    r")\b",
    re.IGNORECASE,
)
_FIXED_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)
_SAME_COHORT = re.compile(r"\b(?:same|identical|that)\s+(?:period|cohort)\b", re.IGNORECASE)
_ORDER_ITEMS_CREATED = re.compile(r"\border\s+items?\s+(?:were\s+)?created\b", re.IGNORECASE)
_REALIZED_METRIC = re.compile(
    r"\b(?:realized|net\s+(?:revenue|sales)|revenue\s+after\s+returns?|"
    r"realized\s+average\s+order\s+value)\b",
    re.IGNORECASE,
)
_PRODUCT_NAME_GRAIN = re.compile(r"\bproduct\s+names?\b", re.IGNORECASE)
_TOP_PRODUCT_NAME_RANKING = re.compile(
    r"\btop\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"product\s+names?\b",
    re.IGNORECASE,
)
_TIE_PRESERVING_RANKING = re.compile(
    r"\b(?:dense\s*rank|preserv(?:e|ing)\s+(?:equal-value\s+)?ties?|"
    r"equal-value\s+ties?)\b",
    re.IGNORECASE,
)
_COMPLETED_ORDER_METRIC = re.compile(r"\bcompleted[- ]orders?\b", re.IGNORECASE)
_SINGLE_REALIZED_TOTAL = re.compile(
    r"^\s*(?:report|calculate|compute|give\s+me|show\s+me|"
    r"what\s+(?:is|are|was|were)|how\s+much(?:\s+(?:is|are|was|were))?)\s+"
    r"(?:the\s+)?(?:total\s+)?(?:realized|net\s+(?:revenue|sales))\b",
    re.IGNORECASE,
)
_GROUPED_SCOPE = re.compile(
    r"\b(?:by|per|for\s+each|each|every|breakdown|trend|over\s+time|"
    r"compare|comparison|versus|vs\.?|rank|ranking|top|bottom|"
    r"daily|weekly|monthly|quarterly|yearly)\b",
    re.IGNORECASE,
)


def validate_query_semantics(
    sql: str,
    *,
    question: str,
    prior_sql: str | None = None,
    reference_date: date | None = None,
) -> None:
    """Reject a small set of high-confidence cohort contradictions.

    This deliberately does not try to prove that arbitrary SQL answers arbitrary
    language. It enforces only explicit temporal constraints whose violation would
    silently change the reviewer-visible cohort.
    """

    if _uses_relative_period(question):
        if reference_date is not None and "current_date" in sql.casefold():
            raise QuerySemanticError(
                "Use deterministic half-open date bounds resolved from the runtime UTC "
                f"date {reference_date.isoformat()}, not CURRENT_DATE()."
            )
        if reference_date is None and "current_date" not in sql.casefold():
            raise QuerySemanticError(
                "The question uses a relative period. Express its bounds with "
                "CURRENT_DATE() and DATE_TRUNC instead of hard-coded dates."
            )

    if _REALIZED_METRIC.search(question) and not _uses_realized_status_policy(sql):
        raise QuerySemanticError(
            "Realized metrics must consistently exclude both 'Cancelled' and 'Returned' "
            "order items from the requested measure."
        )

    if _requests_single_realized_total(question) and _outer_query_adds_breakdown(sql):
        raise QuerySemanticError(
            "The question requests one realized-sales total, not a breakdown or "
            "additional measures. Return exactly one aggregate column and one row; "
            "do not add an unrequested metric, time grain, or dimension grouping."
        )

    if _COMPLETED_ORDER_METRIC.search(question) and not _uses_exact_complete_status(sql):
        raise QuerySemanticError(
            "A completed-order metric must count only rows whose status is exactly "
            "'Complete'. Use status = 'Complete' (or an equivalent conditional count), "
            "not the broader realized-status policy."
        )

    if _PRODUCT_NAME_GRAIN.search(question) and _groups_by_product_identifier(sql):
        raise QuerySemanticError(
            "The requested entity grain is product name. Group by product name without "
            "splitting equal names by products.id or product_id."
        )

    if _TOP_PRODUCT_NAME_RANKING.search(question):
        if _TIE_PRESERVING_RANKING.search(question):
            if not _has_tie_preserving_product_dense_rank(sql):
                raise QuerySemanticError(
                    "A tie-preserving product ranking must use DENSE_RANK ordered only "
                    "by the requested metric. Do not put product name inside the ranking "
                    "window because that breaks equal-value ties."
                )
            if not _has_final_rank_product_name_sort(sql):
                raise QuerySemanticError(
                    "Keep tie rows deterministic by ordering the final result by rank "
                    "and then product name, outside the DENSE_RANK window."
                )
        elif not _has_product_name_rank_tiebreak(sql):
            raise QuerySemanticError(
                "A top product-name ranking needs a deterministic secondary product-name "
                "sort inside the ranking window, after the requested metric."
            )

    if _ORDER_ITEMS_CREATED.search(question) and not _uses_order_items_created_at(sql):
        raise QuerySemanticError(
            "The requested cohort is based on when order items were created. Filter "
            "order_items.created_at (for example oi.created_at), not orders.created_at."
        )

    if prior_sql is None or not _SAME_COHORT.search(question):
        return

    if "current_date" in prior_sql.casefold() and "current_date" not in sql.casefold():
        raise QuerySemanticError(
            "The prior verified cohort used dynamic relative-date bounds. Preserve "
            "those CURRENT_DATE()/DATE_TRUNC bounds for this same-cohort follow-up."
        )
    if _uses_order_items_created_at(prior_sql) and not _uses_order_items_created_at(sql):
        raise QuerySemanticError(
            "The prior verified cohort used order_items.created_at. Preserve that "
            "source timestamp for this same-cohort follow-up."
        )


def _uses_relative_period(question: str) -> bool:
    return bool(_RELATIVE_PERIOD.search(question)) and not bool(_FIXED_DATE.search(question))


def _requests_single_realized_total(question: str) -> bool:
    return bool(_SINGLE_REALIZED_TOTAL.search(question)) and not bool(
        _GROUPED_SCOPE.search(question)
    )


def _outer_query_adds_breakdown(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False

    while isinstance(expression, exp.Subquery):
        expression = expression.this
    return isinstance(expression, exp.Select) and (
        expression.args.get("group") is not None or len(expression.expressions) != 1
    )


def _uses_order_items_created_at(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False

    aliases = normalized_table_aliases(expression)
    direct_tables = set(aliases.values())
    for column in expression.find_all(exp.Column):
        if column.name.casefold() != "created_at":
            continue
        qualifier = column.table.casefold() if column.table else ""
        if qualifier and aliases.get(qualifier) == "order_items":
            return True
        if not qualifier and direct_tables == {"order_items"}:
            return True
    return False


def _uses_realized_status_policy(sql: str) -> bool:
    normalized = " ".join(sql.casefold().split())
    return (
        "cancelled" in normalized
        and "returned" in normalized
        and ("not in" in normalized or "!=" in normalized or "<>" in normalized)
    )


def _uses_exact_complete_status(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False

    def is_status_column(node: exp.Expression) -> bool:
        return isinstance(node, exp.Column) and node.name.casefold() == "status"

    def is_complete_literal(node: exp.Expression) -> bool:
        return (
            isinstance(node, exp.Literal)
            and node.is_string
            and str(node.this).casefold() == "complete"
        )

    for equality in expression.find_all(exp.EQ):
        if (
            is_status_column(equality.this)
            and is_complete_literal(equality.expression)
            or is_complete_literal(equality.this)
            and is_status_column(equality.expression)
        ):
            return True

    for inclusion in expression.find_all(exp.In):
        values = inclusion.expressions
        if (
            is_status_column(inclusion.this)
            and len(values) == 1
            and is_complete_literal(values[0])
        ):
            return True
    return False


def _groups_by_product_identifier(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False
    group = expression.find(exp.Group)
    if group is None:
        return False
    aliases = normalized_table_aliases(expression)
    return any(
        column.name.casefold() == "product_id"
        or (
            column.name.casefold() == "id"
            and aliases.get(column.table.casefold()) == "products"
        )
        for group_expression in group.expressions
        for column in group_expression.find_all(exp.Column)
    )


def _has_product_name_rank_tiebreak(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False
    for window in expression.find_all(exp.Window):
        if not isinstance(window.this, (exp.DenseRank, exp.Rank, exp.RowNumber)):
            continue
        order = window.args.get("order")
        if order is None:
            continue
        if any(
            column.name.casefold() in {"name", "product_name"}
            for column in order.find_all(exp.Column)
        ):
            return True
    return False


def _has_tie_preserving_product_dense_rank(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False
    for window in expression.find_all(exp.Window):
        if not isinstance(window.this, exp.DenseRank):
            continue
        order = window.args.get("order")
        if order is None or not order.expressions:
            continue
        if not any(order_expression.args.get("desc") for order_expression in order.expressions):
            continue
        if any(
            column.name.casefold() in {"name", "product_name"}
            for column in order.find_all(exp.Column)
        ):
            continue
        return True
    return False


def _has_final_rank_product_name_sort(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError:
        return False
    order = expression.args.get("order")
    if order is None:
        return False
    ordered_columns = [
        column.name.casefold()
        for ordered in order.expressions
        for column in ordered.find_all(exp.Column)
    ]
    rank_index = next(
        (index for index, name in enumerate(ordered_columns) if "rank" in name),
        None,
    )
    product_index = next(
        (
            index
            for index, name in enumerate(ordered_columns)
            if name in {"name", "product_name"}
        ),
        None,
    )
    return rank_index is not None and product_index is not None and rank_index < product_index
