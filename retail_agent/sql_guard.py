from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from retail_agent.config import AgentConfig


class SQLSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class SQLValidation:
    original_sql: str
    safe_sql: str
    tables: list[str]


WRITE_EXPRESSIONS = (
    exp.Alter,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.TruncateTable,
    exp.Update,
)


def validate_and_prepare_sql(sql: str, config: AgentConfig) -> SQLValidation:
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except SqlglotError as exc:
        raise SQLSafetyError(f"SQL parse failed: {exc}") from exc

    if len(statements) != 1:
        raise SQLSafetyError("Only one SQL statement is allowed.")

    expression = statements[0]
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise SQLSafetyError("Only SELECT queries are allowed.")

    if any(expression.find_all(*WRITE_EXPRESSIONS)):
        raise SQLSafetyError("DML and DDL statements are forbidden.")

    if any(_is_projection_star(star) for star in expression.find_all(exp.Star)):
        raise SQLSafetyError("SELECT * is forbidden; project only required columns.")

    _validate_query_shape(expression)

    cte_names = _extract_cte_names(expression)
    table_refs = _extract_table_references(expression, cte_names)
    allowed = {
        f"{config.bigquery.dataset}.{table}".lower() for table in config.bigquery.allowed_tables
    }
    unknown = [ref.full_name for ref in table_refs if ref.full_name.lower() not in allowed]
    if unknown:
        raise SQLSafetyError(
            f"Query references disallowed tables: {', '.join(sorted(set(unknown)))}."
        )

    _validate_column_safety(expression, table_refs, cte_names, config)

    calendar_dates_normalized = _normalize_calendar_date_comparisons(expression)
    prepared_sql = (
        expression.sql(dialect="bigquery") if calendar_dates_normalized else sql.strip()
    )
    if calendar_dates_normalized:
        prepared_sql = re.sub(
            r"\bINTERVAL\s+'(\d+)'\s+",
            r"INTERVAL \1 ",
            prepared_sql,
            flags=re.IGNORECASE,
        )
    safe_sql = _validate_limit(prepared_sql, config.bigquery.max_result_rows)
    return SQLValidation(
        original_sql=sql,
        safe_sql=safe_sql,
        tables=[ref.name for ref in table_refs],
    )


def _normalize_calendar_date_comparisons(expression: exp.Expression) -> bool:
    """Coerce known warehouse timestamp columns for calendar-date comparisons."""

    changed = False
    comparison_types = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)
    for comparison in expression.find_all(*comparison_types):
        left = comparison.args.get("this")
        right = comparison.args.get("expression")
        if _is_timestamp_column(left) and _is_calendar_date_expression(right):
            comparison.set("this", exp.Date(this=left.copy()))
            changed = True
        elif _is_calendar_date_expression(left) and _is_timestamp_column(right):
            comparison.set("expression", exp.Date(this=right.copy()))
            changed = True

    for between in expression.find_all(exp.Between):
        value = between.args.get("this")
        low = between.args.get("low")
        high = between.args.get("high")
        if (
            _is_timestamp_column(value)
            and _is_calendar_date_expression(low)
            and _is_calendar_date_expression(high)
        ):
            between.set("this", exp.Date(this=value.copy()))
            changed = True
    return changed


def _is_timestamp_column(expression: exp.Expression | None) -> bool:
    return isinstance(expression, exp.Column) and expression.name.casefold().endswith("_at")


def _is_calendar_date_expression(expression: exp.Expression | None) -> bool:
    if isinstance(
        expression,
        (exp.CurrentDate, exp.Date, exp.DateAdd, exp.DateSub, exp.DateTrunc),
    ):
        return True
    return bool(
        isinstance(expression, exp.Cast)
        and isinstance(expression.to, exp.DataType)
        and expression.to.this is exp.DataType.Type.DATE
    )


def _validate_query_shape(expression: exp.Expression) -> None:
    for join in expression.find_all(exp.Join):
        if join.kind.casefold() == "cross" or not (
            join.args.get("on") is not None or join.args.get("using")
        ):
            raise SQLSafetyError("Every table join requires an explicit non-CROSS join condition.")

    for division in expression.find_all(exp.Div):
        denominator = division.args.get("expression")
        if not _division_denominator_is_guarded(denominator):
            raise SQLSafetyError(
                "Division denominators must use SAFE_DIVIDE, NULLIF(..., 0), "
                "or a non-zero numeric literal."
            )


def _division_denominator_is_guarded(denominator: exp.Expression | None) -> bool:
    if isinstance(denominator, exp.Nullif):
        fallback = denominator.args.get("expression")
        return (
            isinstance(fallback, exp.Literal)
            and not fallback.is_string
            and str(fallback.this) in {"0", "0.0"}
        )
    if isinstance(denominator, exp.Literal) and not denominator.is_string:
        try:
            return float(str(denominator.this)) != 0
        except ValueError:
            return False
    return False


@dataclass(frozen=True)
class TableReference:
    name: str
    full_name: str
    alias: str | None = None


def _extract_cte_names(expression: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias_or_name}


def _extract_table_references(
    expression: exp.Expression, cte_names: set[str]
) -> list[TableReference]:
    tables: list[TableReference] = []
    for table in expression.find_all(exp.Table):
        if _is_cte_reference(table, cte_names):
            continue
        table_ref = _table_reference(table)
        if table_ref is not None:
            tables.append(table_ref)
    return tables


def _table_reference(table: exp.Table) -> TableReference | None:
    name = table.name
    if not name:
        return None
    return TableReference(
        name=name,
        full_name=_fully_qualified_name(table),
        alias=table.alias or None,
    )


def _is_cte_reference(table: exp.Table, cte_names: set[str]) -> bool:
    return (
        bool(table.name) and table.name.lower() in cte_names and not table.db and not table.catalog
    )


def _fully_qualified_name(table: exp.Table) -> str:
    parts = [part.name for part in table.parts if part.name]
    return ".".join(parts)


def _is_projection_star(star: exp.Star) -> bool:
    parent = star.parent
    while parent is not None:
        if isinstance(parent, exp.Count):
            return False
        if isinstance(parent, exp.Select):
            return True
        parent = parent.parent
    return True


def _validate_column_safety(
    expression: exp.Expression,
    table_refs: list[TableReference],
    cte_names: set[str],
    config: AgentConfig,
) -> None:
    pii_columns = {column.lower() for column in config.safety.pii_columns}
    safe_columns = {
        table.lower(): {column.lower() for column in columns}
        for table, columns in config.safety.safe_columns_by_table.items()
    }
    table_aliases = _table_alias_map(table_refs)

    for column in expression.find_all(exp.Column):
        column_name = column.name.lower() if column.name else ""
        qualifier = column.table.lower() if column.table else ""
        if not column_name:
            continue

        if not qualifier and _is_select_alias_reference(column):
            continue

        if not qualifier and column_name in table_aliases:
            ref = table_aliases[column_name]
            raise SQLSafetyError(
                "Query uses forbidden row projection from table alias "
                f"`{column_name}` for `{ref.full_name}`. Project approved columns "
                "explicitly instead."
            )

        if qualifier:
            ref = table_aliases.get(qualifier)
            if ref is not None:
                _ensure_safe_column(column_name, ref, safe_columns, pii_columns)
            continue

        direct_refs = _direct_table_references_for_column(column, cte_names)
        if len(direct_refs) == 1:
            _ensure_safe_column(column_name, direct_refs[0], safe_columns, pii_columns)
        elif len(direct_refs) > 1:
            raise SQLSafetyError(
                f"Unqualified column `{column_name}` is ambiguous across multiple "
                "allowed tables. Qualify it with a table alias."
            )
        elif column_name in pii_columns:
            raise SQLSafetyError(f"Query references forbidden PII columns: {column_name}.")


def _ensure_safe_column(
    column_name: str,
    table_ref: TableReference,
    safe_columns: dict[str, set[str]],
    pii_columns: set[str],
) -> None:
    if column_name in pii_columns:
        raise SQLSafetyError(
            f"Query references forbidden PII columns: {table_ref.name}.{column_name}."
        )

    allowed_columns = safe_columns.get(table_ref.name.lower())
    if not allowed_columns:
        raise SQLSafetyError(
            f"No safe column allowlist is configured for table `{table_ref.name}`."
        )
    if column_name not in allowed_columns:
        raise SQLSafetyError(
            f"Query references forbidden column `{table_ref.name}.{column_name}`. "
            "Only approved non-PII columns may be projected or filtered."
        )


def _table_alias_map(table_refs: list[TableReference]) -> dict[str, TableReference]:
    aliases: dict[str, TableReference] = {}
    for ref in table_refs:
        aliases[ref.name.lower()] = ref
        if ref.alias:
            aliases[ref.alias.lower()] = ref
    return aliases


def normalized_table_aliases(expression: exp.Expression) -> dict[str, str]:
    cte_names = _extract_cte_names(expression)
    table_refs = _extract_table_references(expression, cte_names)
    return {
        alias: reference.name.lower() for alias, reference in _table_alias_map(table_refs).items()
    }


def _direct_table_references_for_column(
    column: exp.Column, cte_names: set[str]
) -> list[TableReference]:
    select = column.find_ancestor(exp.Select)
    if select is None:
        return []
    return _direct_table_references(select, cte_names)


def _direct_table_references(select: exp.Select, cte_names: set[str]) -> list[TableReference]:
    refs: list[TableReference] = []
    from_expression = select.args.get("from_")
    if from_expression is not None:
        refs.extend(_direct_table_references_from_expression(from_expression, cte_names))
    for join in select.args.get("joins") or []:
        refs.extend(_direct_table_references_from_expression(join, cte_names))
    return refs


def _direct_table_references_from_expression(
    expression: exp.Expression, cte_names: set[str]
) -> list[TableReference]:
    candidates: list[exp.Expression] = []
    if isinstance(expression, exp.Table):
        candidates.append(expression)
    if expression.args.get("this") is not None:
        candidates.append(expression.args["this"])
    candidates.extend(expression.args.get("expressions") or [])

    refs: list[TableReference] = []
    for candidate in candidates:
        if not isinstance(candidate, exp.Table):
            continue
        if _is_cte_reference(candidate, cte_names):
            continue
        table_ref = _table_reference(candidate)
        if table_ref is not None:
            refs.append(table_ref)
    return refs


def _is_select_alias_reference(column: exp.Column) -> bool:
    select = column.find_ancestor(exp.Select)
    if select is None:
        return False

    aliases = {
        expression.alias.lower()
        for expression in select.args.get("expressions") or []
        if isinstance(expression, exp.Alias) and expression.alias
    }
    if column.name.lower() not in aliases:
        return False

    projection_expressions = set(select.args.get("expressions") or [])
    parent = column.parent
    while parent is not None and parent is not select:
        if parent in projection_expressions:
            return False
        parent = parent.parent
    return True


def _validate_limit(sql: str, max_rows: int) -> str:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError as exc:
        raise SQLSafetyError(f"SQL parse failed: {exc}") from exc
    limit = _top_level_limit_value(expression)
    if limit is not None:
        if limit > max_rows:
            raise SQLSafetyError(f"LIMIT {limit} exceeds maximum row limit {max_rows}.")
    return sql.rstrip(";")


def _top_level_limit_value(expression: exp.Expression) -> int | None:
    limit = expression.args.get("limit")
    if limit is None:
        return None
    value = limit.args.get("expression")
    if not isinstance(value, exp.Literal) or value.is_string:
        raise SQLSafetyError("Queries must use a numeric literal LIMIT.")
    try:
        return int(str(value.this))
    except ValueError as exc:
        raise SQLSafetyError("Queries must use a numeric literal LIMIT.") from exc
