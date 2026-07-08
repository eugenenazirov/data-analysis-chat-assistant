from __future__ import annotations

from dataclasses import dataclass

from retail_agent.config import AgentConfig
from retail_agent.pii import redact_text
from retail_agent.sql_guard import SQLSafetyError, validate_and_prepare_sql


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    detail: str


def run_guardrail_evals(config: AgentConfig) -> list[EvalResult]:
    return [
        _eval_safe_sql(config),
        _eval_pii_sql_block(config),
        _eval_dml_block(config),
        _eval_pii_redaction(),
        _eval_limit_added(config),
    ]


def _eval_safe_sql(config: AgentConfig) -> EvalResult:
    sql = """
    SELECT product_id, COUNT(*) AS item_count
    FROM `bigquery-public-data.thelook_ecommerce.order_items`
    GROUP BY product_id
    ORDER BY item_count DESC
    LIMIT 10
    """
    try:
        validate_and_prepare_sql(sql, config)
    except Exception as exc:
        return EvalResult("safe_sql_allowed", False, str(exc))
    return EvalResult("safe_sql_allowed", True, "safe aggregate SQL accepted")


def _eval_pii_sql_block(config: AgentConfig) -> EvalResult:
    sql = "SELECT email FROM `bigquery-public-data.thelook_ecommerce.users` LIMIT 10"
    try:
        validate_and_prepare_sql(sql, config)
    except SQLSafetyError:
        return EvalResult("pii_sql_blocked", True, "PII column blocked")
    return EvalResult("pii_sql_blocked", False, "PII column was not blocked")


def _eval_dml_block(config: AgentConfig) -> EvalResult:
    sql = "DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE id = 1"
    try:
        validate_and_prepare_sql(sql, config)
    except SQLSafetyError:
        return EvalResult("dml_blocked", True, "destructive SQL blocked")
    return EvalResult("dml_blocked", False, "destructive SQL was not blocked")


def _eval_pii_redaction() -> EvalResult:
    text, redactions = redact_text("Email jane@example.com or call +1 415-555-0123")
    passed = "[REDACTED_EMAIL]" in text and "[REDACTED_PHONE]" in text and redactions == 2
    return EvalResult("pii_output_redacted", passed, text)


def _eval_limit_added(config: AgentConfig) -> EvalResult:
    sql = """
    SELECT product_id, COUNT(*) AS item_count
    FROM `bigquery-public-data.thelook_ecommerce.order_items`
    GROUP BY product_id
    ORDER BY item_count DESC
    """
    validation = validate_and_prepare_sql(sql, config)
    passed = f"LIMIT {config.bigquery.max_result_rows}" in validation.safe_sql
    return EvalResult("limit_added", passed, validation.safe_sql)
