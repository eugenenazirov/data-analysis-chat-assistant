from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Literal

from retail_agent.domain.models.analysis import NARRATIVE_OUTPUT_RULE as NARRATIVE_OUTPUT_RULE

type NarrativeOutputViolation = Literal["markdown_table", "row_dump"]

_MARKDOWN_TABLE_PATTERN = re.compile(
    r"(?m)^\s*\|?.+\|.+\|?\s*$\n"
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_MIN_DUMPED_ROWS = 3


def narrative_output_violation(
    fragments: Iterable[str],
    rows: list[dict[str, Any]],
) -> NarrativeOutputViolation | None:
    """Detect table-shaped or repeated-row narrative that duplicates verified rows."""

    parts = [fragment for fragment in fragments if fragment.strip()]
    text = "\n".join(parts)
    if _MARKDOWN_TABLE_PATTERN.search(text):
        return "markdown_table"
    if len(rows) < _MIN_DUMPED_ROWS:
        return None

    row_tokens = [_row_tokens(row) for row in rows]
    value_frequencies = Counter(token for tokens in row_tokens for token in tokens)
    matched_rows: set[int] = set()
    for line in text.splitlines():
        normalized_line = re.sub(r"(?<=\d),(?=\d)", "", line.casefold())
        row_matches = [
            index
            for index, tokens in enumerate(row_tokens)
            if _line_reproduces_row(normalized_line, tokens, value_frequencies)
        ]
        if len(row_matches) == 1:
            matched_rows.add(row_matches[0])
            if len(matched_rows) >= _MIN_DUMPED_ROWS:
                return "row_dump"
    return None


def _row_tokens(row: dict[str, Any]) -> set[str]:
    return {
        token
        for value in row.values()
        if (token := _value_token(value)) is not None
    }


def _line_reproduces_row(
    normalized_line: str,
    tokens: set[str],
    value_frequencies: Counter[str],
) -> bool:
    matched = {token for token in tokens if _token_in_text(token, normalized_line)}
    return len(matched) >= 2 and any(value_frequencies[token] == 1 for token in matched)


def _value_token(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    token = _numeric_token(value) if isinstance(value, (int, float, Decimal)) else str(value)
    token = token.strip().casefold()
    return token if len(token) >= 2 else None


def _numeric_token(value: int | float | Decimal) -> str:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal_value.is_finite():
        return ""
    token = format(decimal_value, "f")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return "0" if token == "-0" else token


def _token_in_text(token: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w.]){re.escape(token)}(?![\w.])", text))
