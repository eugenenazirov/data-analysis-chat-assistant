from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from retail_agent.domain.models import AnalysisReport

NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(?:(?:EUR|GBP|JPY|USD|[$€£¥])\s*)?"
    r"[-+]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:%|[KMBkmb](?![A-Za-z])|thousand|million|billion))?",
    re.IGNORECASE,
)
MAX_DERIVATION_VALUES_PER_MEASURE = 100
MAX_MEASURE_MENTION_DISTANCE = 48
MONETARY_MEASURE_TOKENS = frozenset({"amount", "cost", "price", "revenue", "sales", "spend"})
RATE_MEASURE_TOKENS = frozenset({"percentage", "rate", "ratio"})
IDENTIFIER_SEPARATOR_PATTERN = r"\s*(?:[:#-]\s*)?"
type _ContextKind = Literal["interval", "limit", "month", "year"]
type _DerivationCache = dict[tuple[str, bool, bool], tuple[float, ...]]


@dataclass(frozen=True)
class _ContextNumber:
    value: float
    kind: _ContextKind
    unit: str | None = None


@dataclass(frozen=True)
class _NumericClaim:
    match: re.Match[str]
    value: float
    tolerance: float
    measure_names: frozenset[str]
    currency: bool
    percentage: bool
    scaled: bool


@dataclass(frozen=True)
class ReportEvidenceAssessment:
    score: float
    unsupported_numeric_claims: tuple[float, ...]

    @property
    def is_supported(self) -> bool:
        return self.score == 1.0


def assess_report_evidence(
    report: AnalysisReport,
    rows: list[dict[str, Any]],
    sql: str,
    tolerance: float = 0.001,
    *,
    reference_date: date | None = None,
) -> ReportEvidenceAssessment:
    score, unsupported = _faithfulness_details(
        report,
        rows,
        sql,
        tolerance,
        reference_date or datetime.now(UTC).date(),
    )
    return ReportEvidenceAssessment(score, tuple(unsupported))


def report_uses_verified_sql(report: AnalysisReport, verified_sql: str) -> bool:
    return report.sql == verified_sql


def _name_words(name: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", name.lower()))


def _faithfulness_details(
    report: AnalysisReport,
    rows: list[dict[str, Any]],
    sql: str,
    tolerance: float,
    reference_date: date,
) -> tuple[float, list[float]]:
    text = " ".join([report.answer, *report.highlights])
    claim_matches = list(NUMBER_PATTERN.finditer(text))
    if not claim_matches:
        return 1.0, []
    dimension_spans = _returned_numeric_dimension_spans(text, rows)
    dimension_counts = _returned_dimension_counts(rows)
    count_nouns = _returned_count_nouns(rows)
    measures: dict[str, list[float]] = defaultdict(list)
    numeric_identifiers: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for column, value in row.items():
            if isinstance(value, (int, float)):
                target = numeric_identifiers if _is_identifier_column(column) else measures
                target[column].append(float(value))
    claims = [_build_numeric_claim(text, match, measures, tolerance) for match in claim_matches]
    context_values = _supported_context_numbers(sql, reference_date)
    derivation_cache: _DerivationCache = {}
    if (
        not measures
        and not numeric_identifiers
        and not context_values
        and not dimension_counts
    ):
        return 0.0, [claim.value for claim in claims]
    unsupported = [
        claim.value
        for claim in claims
        if not _claim_is_returned_dimension(claim.match, dimension_spans)
        and not _claim_is_derived_count(
            text,
            claim,
            row_count=len(rows),
            dimension_counts=dimension_counts,
            count_nouns=count_nouns,
        )
        and not _claim_is_numeric_identifier(text, claim, numeric_identifiers)
        and not _claim_supported(
            claim,
            measures,
            context_values,
            text,
            derivation_cache,
        )
    ]
    return (
        (len(claim_matches) - len(unsupported)) / len(claim_matches),
        unsupported,
    )


def _build_numeric_claim(
    text: str,
    match: re.Match[str],
    measures: dict[str, list[float]],
    tolerance: float,
) -> _NumericClaim:
    currency = _is_currency_claim(text, match)
    return _NumericClaim(
        match=match,
        value=_parse_number(match.group()),
        tolerance=max(tolerance, _claim_text_tolerance(text, match)),
        measure_names=frozenset(
            _measure_names_for_claim(
                text,
                match,
                measures,
                currency_claim=currency,
            )
        ),
        currency=currency,
        percentage=_is_percentage_claim(text, match),
        scaled=_is_scaled_claim(match),
    )


def _returned_numeric_dimension_spans(
    text: str, rows: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    values = {
        value.strip()
        for row in rows
        for value in row.values()
        if isinstance(value, str)
        and any(character.isdigit() for character in value)
        and any(character.isalpha() for character in value)
        and value.strip()
    }
    spans: list[tuple[int, int]] = []
    for value in values:
        flexible_value = r"\s+".join(re.escape(part) for part in value.split())
        pattern = re.compile(rf"(?<!\w){flexible_value}(?!\w)", re.IGNORECASE)
        spans.extend(match.span() for match in pattern.finditer(text))
    return spans


def _claim_is_returned_dimension(
    claim: re.Match[str], dimension_spans: list[tuple[int, int]]
) -> bool:
    return any(start <= claim.start() and claim.end() <= end for start, end in dimension_spans)


def _returned_dimension_counts(rows: list[dict[str, Any]]) -> Counter[tuple[str, str]]:
    return Counter(
        (column.casefold(), value.strip().casefold())
        for row in rows
        for column, value in row.items()
        if isinstance(value, str) and value.strip()
    )


def _returned_count_nouns(rows: list[dict[str, Any]]) -> frozenset[str]:
    nouns = {"entries", "results", "rows"}
    column_words = {
        word
        for row in rows
        for column in row
        for word in _name_words(column)
    }
    entity_nouns = {
        "category": "categories",
        "customer": "customers",
        "item": "items",
        "product": "products",
    }
    nouns.update(plural for entity, plural in entity_nouns.items() if entity in column_words)
    return frozenset(nouns)


def _claim_is_derived_count(
    text: str,
    claim: _NumericClaim,
    *,
    row_count: int,
    dimension_counts: Counter[tuple[str, str]],
    count_nouns: frozenset[str],
) -> bool:
    if claim.currency or claim.percentage or claim.scaled or not claim.value.is_integer():
        return False
    claimed_count = int(claim.value)
    prefix = text[max(0, claim.match.start() - 120) : claim.match.start()].casefold()
    suffix = text[claim.match.end() : min(len(text), claim.match.end() + 80)].casefold()
    noun_pattern = "|".join(sorted(map(re.escape, count_nouns)))
    if claimed_count == row_count and re.match(
        rf"^\s*(?:returned\s+|listed\s+|matching\s+)?(?:{noun_pattern})\b",
        suffix,
    ):
        return True

    denominator = re.match(
        rf"^\s+out\s+of\s+{row_count}\s+"
        rf"(?:returned\s+|listed\s+|matching\s+)?(?:{noun_pattern})\b",
        suffix,
    )
    if denominator is None:
        return False
    return any(
        count == claimed_count
        and re.search(rf"(?<!\w){re.escape(dimension)}(?!\w)", prefix)
        for (_, dimension), count in dimension_counts.items()
    )


def _is_identifier_column(column: str) -> bool:
    normalized = column.lower()
    return normalized == "id" or normalized.endswith("_id")


def _claim_is_numeric_identifier(
    text: str,
    claim: _NumericClaim,
    identifiers: dict[str, list[float]],
) -> bool:
    if not identifiers or claim.currency or claim.percentage or claim.scaled:
        return False

    matching_columns = {
        column
        for column, values in identifiers.items()
        if any(claim.value == identifier for identifier in values)
    }
    if not matching_columns:
        return False

    prefix = text[max(0, claim.match.start() - 40) : claim.match.start()].lower()
    if len(identifiers) == 1 and re.search(rf"\bid{IDENTIFIER_SEPARATOR_PATTERN}$", prefix):
        return True
    return any(_identifier_entity_prefix_matches(prefix, column) for column in matching_columns)


def _identifier_entity_prefix_matches(prefix: str, column: str) -> bool:
    entity = " ".join(_name_words(column.removesuffix("_id")))
    if not entity:
        return False
    aliases = {entity}
    if not entity.endswith("s"):
        aliases.add(f"{entity}s")
    return any(
        re.search(
            rf"\b{re.escape(alias)}(?:\s*(?:\(\s*)?id)?"
            rf"{IDENTIFIER_SEPARATOR_PATTERN}$",
            prefix,
        )
        for alias in aliases
    )


def _supported_context_numbers(sql: str, reference_date: date) -> list[_ContextNumber]:
    values = [
        _ContextNumber(
            value=float(raw),
            kind="limit",
        )
        for raw in re.findall(r"\bLIMIT\s+(\d+)\b", sql, re.IGNORECASE)
    ]
    values.extend(
        _ContextNumber(
            value=float(raw),
            kind="interval",
            unit=unit.lower(),
        )
        for raw, unit in re.findall(
            r"\bINTERVAL\s+(\d+)\s+(DAY|WEEK|MONTH|QUARTER|YEAR)\b",
            sql,
            re.IGNORECASE,
        )
    )
    if "CURRENT_DATE" in sql.upper():
        previous_month = reference_date.replace(day=1) - timedelta(days=1)
        values.extend(
            [
                _ContextNumber(float(reference_date.year), "year"),
                _ContextNumber(float(reference_date.month), "month"),
                _ContextNumber(float(previous_month.year), "year"),
                _ContextNumber(float(previous_month.month), "month"),
            ]
        )
    return values


def _claim_supported(
    claim: _NumericClaim,
    measures: dict[str, list[float]],
    context_values: list[_ContextNumber],
    text: str,
    derivation_cache: _DerivationCache,
) -> bool:
    if _context_claim_supported(claim, text, context_values):
        return True

    if claim.currency and claim.percentage:
        return False

    measure_names = set(claim.measure_names)
    if claim.currency:
        monetary_measures = _measures_with_tokens(measures, MONETARY_MEASURE_TOKENS)
        if not monetary_measures:
            return False
        measure_names &= monetary_measures
        if not measure_names:
            return False

    if not measure_names:
        if len(measures) != 1:
            return False
        measure_names = set(measures)

    if claim.percentage:
        return _percentage_claim_supported(
            claim.value,
            measures,
            measure_names,
            claim.tolerance,
            derivation_cache,
        )

    raw_values = [value for name in measure_names for value in measures[name]]
    if any(_numbers_match(claim.value, value, claim.tolerance) for value in raw_values):
        return True

    for name in measure_names:
        derivations = _cached_measure_derivations(
            measures,
            name,
            include_additive=True,
            include_ratios=not claim.currency,
            cache=derivation_cache,
        )
        if any(_numbers_match(claim.value, value, claim.tolerance) for value in derivations):
            return True
    return False


def _percentage_claim_supported(
    claim: float,
    measures: dict[str, list[float]],
    measure_names: set[str],
    tolerance: float,
    derivation_cache: _DerivationCache,
) -> bool:
    normalized_claim = claim / 100
    rate_measures = measure_names & _measures_with_tokens(measures, RATE_MEASURE_TOKENS)
    if any(
        _numbers_match(normalized_claim, value, tolerance)
        for name in rate_measures
        for value in measures[name]
    ):
        return True

    return any(
        _numbers_match(normalized_claim, value, tolerance)
        for name in measure_names
        for value in _cached_measure_derivations(
            measures,
            name,
            include_additive=False,
            include_ratios=True,
            cache=derivation_cache,
        )
    )


def _cached_measure_derivations(
    measures: dict[str, list[float]],
    name: str,
    *,
    include_additive: bool,
    include_ratios: bool,
    cache: _DerivationCache,
) -> tuple[float, ...]:
    key = (name, include_additive, include_ratios)
    if key not in cache:
        cache[key] = tuple(
            _same_measure_derivations(
                measures[name],
                include_additive=include_additive,
                include_ratios=include_ratios,
            )
        )
    return cache[key]


def _same_measure_derivations(
    values: list[float],
    *,
    include_additive: bool,
    include_ratios: bool,
) -> Iterator[float]:
    bounded = values[:MAX_DERIVATION_VALUES_PER_MEASURE]
    for left_index, left in enumerate(bounded):
        for right in bounded[left_index + 1 :]:
            if include_additive:
                yield from (left + right, left - right, right - left, abs(left - right))
            if include_ratios and right:
                yield from (left / right, (left - right) / right)
            if include_ratios and left:
                yield from (right / left, (right - left) / left)


def _context_claim_supported(
    claim: _NumericClaim,
    text: str,
    context_values: list[_ContextNumber],
) -> bool:
    if claim.currency or claim.percentage or claim.scaled:
        return False
    for context in context_values:
        if _numbers_match(
            claim.value, context.value, claim.tolerance
        ) and _claim_has_context_structure(text, claim.match, context):
            return True
    return False


def _claim_has_context_structure(
    text: str,
    claim: re.Match[str],
    context: _ContextNumber,
) -> bool:
    prefix = text[max(0, claim.start() - 32) : claim.start()].lower()
    suffix = text[claim.end() : min(len(text), claim.end() + 24)].lower()

    if context.kind == "limit":
        return bool(
            re.search(r"\b(?:first|limit(?:\s+of)?|limited\s+to|top)\s*$", prefix)
            or re.match(r"^\s*(?:results?|rows?)\b", suffix)
        )
    if context.kind == "interval" and context.unit is not None:
        unit = re.escape(context.unit.rstrip("s"))
        return bool(re.match(rf"^\s*{unit}s?\b", suffix))
    if context.kind == "year":
        return bool(
            re.search(
                r"\b(?:(?:calendar|fiscal)\s+year|during|fy|in|since|year)\s*$",
                prefix,
            )
            or re.match(r"^\s*(?:(?:calendar|fiscal)\s+year|year|ytd)\b", suffix)
        )
    if context.kind == "month":
        return bool(re.search(r"\bmonth\s*$", prefix) or re.match(r"^\s*months?\b", suffix))
    return False


def _measure_names_for_claim(
    text: str,
    claim: re.Match[str],
    measures: dict[str, list[float]],
    *,
    currency_claim: bool,
) -> set[str]:
    normalized_text = text.lower()
    eligible_measures = set(measures)
    if currency_claim:
        eligible_measures = _measures_with_tokens(measures, MONETARY_MEASURE_TOKENS)

    if len(eligible_measures) == 1:
        return eligible_measures

    candidates: list[tuple[int, int, str]] = []
    for measure_name in eligible_measures:
        for alias in _measure_aliases(measure_name):
            for mention in re.finditer(rf"\b{re.escape(alias)}\b", normalized_text):
                distance = _span_distance(claim.span(), mention.span())
                if distance <= MAX_MEASURE_MENTION_DISTANCE:
                    candidates.append((distance, -len(alias), measure_name))

    if not candidates:
        return set()
    best_distance, best_length, _ = min(candidates)
    return {
        measure_name
        for distance, alias_length, measure_name in candidates
        if (distance, alias_length) == (best_distance, best_length)
    }


def _measure_aliases(measure_name: str) -> set[str]:
    words = _name_words(measure_name)
    aliases = set(words) - {"count", "gross", "id", "number", "total"}
    if words:
        aliases.add(" ".join(words))
    if "orders" in aliases:
        aliases.add("order")
    if "items" in aliases:
        aliases.add("item")
    if "customer" in aliases:
        aliases.add("customers")
    if "spend" in aliases:
        aliases.add("spent")
    if "sales" in aliases:
        aliases.add("sale")
    if "sold" in aliases:
        aliases.add("sell")
    if "return" in aliases:
        aliases.add("returns")
    return aliases


def _measure_tokens(measure_name: str) -> set[str]:
    return set(_name_words(measure_name))


def _measures_with_tokens(measures: dict[str, list[float]], tokens: frozenset[str]) -> set[str]:
    return {name for name in measures if _measure_tokens(name) & tokens}


def _is_currency_claim(text: str, claim: re.Match[str]) -> bool:
    raw = claim.group().lower()
    prefix = text[max(0, claim.start() - 4) : claim.start()].lower()
    suffix = text[claim.end() : min(len(text), claim.end() + 16)].lower()
    return bool(
        re.search(r"(?:[$€£¥]|(?:eur|gbp|jpy|usd))\s*$", prefix)
        or re.match(r"^(?:[$€£¥]|eur|gbp|jpy|usd)", raw)
        or re.match(
            r"^\s*(?:[$€£¥]|(?:dollars?|eur|euros?|gbp|jpy|pounds?|usd|yen)\b)",
            suffix,
        )
    )


def _is_percentage_claim(text: str, claim: re.Match[str]) -> bool:
    suffix = text[claim.end() : min(len(text), claim.end() + 20)].lower()
    return "%" in claim.group() or bool(
        re.match(
            r"^\s*(?:percent|percentage\s+points?)\b",
            suffix,
        )
    )


def _is_scaled_claim(claim: re.Match[str]) -> bool:
    return bool(
        re.search(
            r"(?:[kmb](?![A-Za-z])|thousand|million|billion)\s*$",
            claim.group(),
            re.IGNORECASE,
        )
    )


def _claim_text_tolerance(text: str, claim: re.Match[str]) -> float:
    prefix = text[max(0, claim.start() - 24) : claim.start()].lower()
    if re.search(
        r"(?:\b(?:about|approximately|around|over|roughly)|[~≈])\s*[$€£¥]?\s*$",
        prefix,
    ):
        return 0.01
    return 0.0


def _span_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    if left[1] <= right[0]:
        return right[0] - left[1]
    if right[1] <= left[0]:
        return left[0] - right[1]
    return 0


def _numbers_match(
    claim: float,
    value: float,
    tolerance: float,
) -> bool:
    relative_tolerance = max(tolerance, 0.005)
    return math.isclose(
        claim,
        value,
        rel_tol=relative_tolerance,
        abs_tol=tolerance,
    )


def _parse_number(raw: str) -> float:
    normalized = raw.strip().lower().replace(",", "")
    normalized = re.sub(r"^(?:[$€£¥]|eur|gbp|jpy|usd)\s*", "", normalized)
    multipliers = {
        "k": 1_000,
        "thousand": 1_000,
        "m": 1_000_000,
        "million": 1_000_000,
        "b": 1_000_000_000,
        "billion": 1_000_000_000,
    }
    for suffix, multiplier in multipliers.items():
        if normalized.endswith(suffix):
            return float(normalized[: -len(suffix)].strip()) * multiplier
    return float(normalized.rstrip("%").strip())
