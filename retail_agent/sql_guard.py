from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

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
    statements = sqlglot.parse(sql, read="bigquery")
    if len(statements) != 1:
        raise SQLSafetyError("Only one SQL statement is allowed.")

    expression = statements[0]
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise SQLSafetyError("Only SELECT queries are allowed.")

    if any(expression.find_all(*WRITE_EXPRESSIONS)):
        raise SQLSafetyError("DML and DDL statements are forbidden.")

    if any(_is_projection_star(star) for star in expression.find_all(exp.Star)):
        raise SQLSafetyError("SELECT * is forbidden; project only required columns.")

    tables = _extract_tables(expression)
    allowed = set(config.bigquery.allowed_tables)
    unknown = [table for table in tables if table not in allowed]
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
    return SQLValidation(original_sql=sql, safe_sql=safe_sql, tables=tables)


def _extract_tables(expression: exp.Expression) -> list[str]:
    tables: list[str] = []
    for table in expression.find_all(exp.Table):
        name = table.name
        if name:
            tables.append(name)
    return tables


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
    expression = sqlglot.parse_one(sql, read="bigquery")
    if _has_top_level_limit(expression):
        return sql.rstrip(";")
    return f"{sql.rstrip(';')}\nLIMIT {max_rows}"


def _has_top_level_limit(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Union):
        return expression.args.get("limit") is not None
    return expression.args.get("limit") is not None
