from __future__ import annotations

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

    cte_names = _extract_cte_names(expression)
    table_refs = _extract_table_references(expression, cte_names)
    allowed = {
        f"{config.bigquery.dataset}.{table}".lower()
        for table in config.bigquery.allowed_tables
    }
    unknown = [
        ref.full_name
        for ref in table_refs
        if ref.full_name.lower() not in allowed
    ]
    if unknown:
        raise SQLSafetyError(
            f"Query references disallowed tables: {', '.join(sorted(set(unknown)))}."
        )

    _validate_column_safety(expression, table_refs, cte_names, config)

    safe_sql = _ensure_limit(sql.strip(), config.bigquery.max_result_rows)
    return SQLValidation(
        original_sql=sql,
        safe_sql=safe_sql,
        tables=[ref.name for ref in table_refs],
    )


@dataclass(frozen=True)
class TableReference:
    name: str
    full_name: str
    alias: str | None = None


def _extract_cte_names(expression: exp.Expression) -> set[str]:
    return {
        cte.alias_or_name.lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }


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
        bool(table.name)
        and table.name.lower() in cte_names
        and not table.db
        and not table.catalog
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

        if _is_select_alias_reference(column):
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
            raise SQLSafetyError(
                f"Query references forbidden PII columns: {column_name}."
            )


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


def _direct_table_references_for_column(
    column: exp.Column, cte_names: set[str]
) -> list[TableReference]:
    select = column.find_ancestor(exp.Select)
    if select is None:
        return []
    return _direct_table_references(select, cte_names)


def _direct_table_references(
    select: exp.Select, cte_names: set[str]
) -> list[TableReference]:
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


def _ensure_limit(sql: str, max_rows: int) -> str:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError as exc:
        raise SQLSafetyError(f"SQL parse failed: {exc}") from exc
    limit = _top_level_limit_value(expression)
    if limit is not None:
        if limit > max_rows:
            raise SQLSafetyError(
                f"LIMIT {limit} exceeds maximum row limit {max_rows}."
            )
        return sql.rstrip(";")
    return f"{sql.rstrip(';')}\nLIMIT {max_rows}"


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
