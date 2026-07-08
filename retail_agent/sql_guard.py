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

    table_refs = _extract_table_references(expression)
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

    pii_columns = {column.lower() for column in config.safety.pii_columns}
    referenced_pii = sorted(
        {
            column.name.lower()
            for column in expression.find_all(exp.Column)
            if column.name and column.name.lower() in pii_columns
        }
    )
    if referenced_pii:
        raise SQLSafetyError(
            f"Query references forbidden PII columns: {', '.join(referenced_pii)}."
        )

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


def _extract_table_references(expression: exp.Expression) -> list[TableReference]:
    cte_names = {
        cte.alias_or_name.lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    tables: list[TableReference] = []
    for table in expression.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        if _is_cte_reference(table, cte_names):
            continue
        full_name = _fully_qualified_name(table)
        tables.append(TableReference(name=name, full_name=full_name))
    return tables


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


def _ensure_limit(sql: str, max_rows: int) -> str:
    try:
        expression = sqlglot.parse_one(sql, read="bigquery")
    except SqlglotError as exc:
        raise SQLSafetyError(f"SQL parse failed: {exc}") from exc
    if _has_top_level_limit(expression):
        return sql.rstrip(";")
    return f"{sql.rstrip(';')}\nLIMIT {max_rows}"


def _has_top_level_limit(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Union):
        return expression.args.get("limit") is not None
    return expression.args.get("limit") is not None
