from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from retail_agent.config import AgentConfig
from retail_agent.pii import redact_text
from retail_agent.sql_guard import SQLSafetyError, validate_and_prepare_sql


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GuardrailEvalCase:
    name: str
    kind: str
    sql: str = ""
    text: str = ""
    expected_error: str = ""


class ExpectedPass(Evaluator[GuardrailEvalCase, EvalResult]):
    def evaluate(self, ctx: EvaluatorContext[GuardrailEvalCase, EvalResult]) -> dict[str, bool]:
        expected = ctx.expected_output
        return {"expected_pass": expected is not None and ctx.output.passed is expected.passed}


def run_guardrail_evals(config: AgentConfig) -> list[EvalResult]:
    report = build_guardrail_dataset().evaluate_sync(
        lambda case: _run_guardrail_case(config, case),
        progress=False,
    )
    return [case.output for case in report.cases if case.output is not None]


def build_guardrail_dataset() -> Dataset[GuardrailEvalCase, EvalResult, None]:
    return Dataset(
        name="retail_agent_guardrails",
        cases=[
            _case(
                name="safe_sql_allowed",
                inputs=GuardrailEvalCase(
                    name="safe_sql_allowed",
                    kind="sql_allowed",
                    sql="""
                    SELECT product_id, COUNT(*) AS item_count
                    FROM `bigquery-public-data.thelook_ecommerce.order_items`
                    GROUP BY product_id
                    ORDER BY item_count DESC
                    LIMIT 10
                    """,
                ),
            ),
            _case(
                name="pii_sql_blocked",
                inputs=GuardrailEvalCase(
                    name="pii_sql_blocked",
                    kind="sql_blocked",
                    sql="SELECT email FROM `bigquery-public-data.thelook_ecommerce.users` LIMIT 10",
                    expected_error="PII",
                ),
            ),
            _case(
                name="user_pii_sql_blocked",
                inputs=GuardrailEvalCase(
                    name="user_pii_sql_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT first_name FROM "
                        "`bigquery-public-data.thelook_ecommerce.users` LIMIT 10"
                    ),
                    expected_error="PII",
                ),
            ),
            _case(
                name="row_projection_blocked",
                inputs=GuardrailEvalCase(
                    name="row_projection_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT u FROM `bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 10"
                    ),
                    expected_error="row projection",
                ),
            ),
            _case(
                name="excessive_limit_blocked",
                inputs=GuardrailEvalCase(
                    name="excessive_limit_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT order_id FROM "
                        "`bigquery-public-data.thelook_ecommerce.orders` LIMIT 1000000"
                    ),
                    expected_error="exceeds maximum",
                ),
            ),
            _case(
                name="dml_blocked",
                inputs=GuardrailEvalCase(
                    name="dml_blocked",
                    kind="sql_blocked",
                    sql="DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE id = 1",
                    expected_error="Only SELECT",
                ),
            ),
            _case(
                name="table_scope_blocked",
                inputs=GuardrailEvalCase(
                    name="table_scope_blocked",
                    kind="sql_blocked",
                    sql="SELECT id FROM `other-project.thelook_ecommerce.orders` LIMIT 10",
                    expected_error="disallowed tables",
                ),
            ),
            _case(
                name="malformed_sql_retryable",
                inputs=GuardrailEvalCase(
                    name="malformed_sql_retryable",
                    kind="sql_blocked",
                    sql="SELECT FROM",
                    expected_error="SQL parse failed",
                ),
            ),
            _case(
                name="pii_output_redacted",
                inputs=GuardrailEvalCase(
                    name="pii_output_redacted",
                    kind="redaction",
                    text="Email jane@example.com or call +1 415-555-0123",
                ),
            ),
            _case(
                name="limit_added",
                inputs=GuardrailEvalCase(
                    name="limit_added",
                    kind="limit_added",
                    sql="""
                    SELECT product_id, COUNT(*) AS item_count
                    FROM `bigquery-public-data.thelook_ecommerce.order_items`
                    GROUP BY product_id
                    ORDER BY item_count DESC
                    """,
                ),
            ),
            _case(
                name="alias_star_blocked",
                inputs=GuardrailEvalCase(
                    name="alias_star_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT u.* FROM "
                        "`bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 5"
                    ),
                    expected_error="SELECT *",
                ),
            ),
            _case(
                name="nested_star_blocked",
                inputs=GuardrailEvalCase(
                    name="nested_star_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT AS STRUCT u.* FROM "
                        "`bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 5"
                    ),
                    expected_error="SELECT *",
                ),
            ),
            *[
                _case(
                    name=f"pii_{column}_blocked",
                    inputs=GuardrailEvalCase(
                        name=f"pii_{column}_blocked",
                        kind="sql_blocked",
                        sql=(
                            f"SELECT {column} FROM "
                            "`bigquery-public-data.thelook_ecommerce.users` LIMIT 5"
                        ),
                        expected_error="PII",
                    ),
                )
                for column in (
                    "last_name",
                    "phone",
                    "street_address",
                    "postal_code",
                    "latitude",
                )
            ],
            _case(
                name="stacked_statement_blocked",
                inputs=GuardrailEvalCase(
                    name="stacked_statement_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT order_id FROM "
                        "`bigquery-public-data.thelook_ecommerce.orders` LIMIT 1; "
                        "DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE TRUE"
                    ),
                    expected_error="one SQL statement",
                ),
            ),
            *[
                _case(
                    name=name,
                    inputs=GuardrailEvalCase(
                        name=name,
                        kind="sql_blocked",
                        sql=sql,
                        expected_error="Only SELECT",
                    ),
                )
                for name, sql in (
                    ("create_table_blocked", "CREATE TABLE scratch AS SELECT 1"),
                    (
                        "update_with_mixed_case_blocked",
                        "UpDaTe `bigquery-public-data.thelook_ecommerce.orders` "
                        "SET status = 'x' WHERE TRUE",
                    ),
                )
            ],
            _case(
                name="cross_join_blocked",
                inputs=GuardrailEvalCase(
                    name="cross_join_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT o.order_id FROM `bigquery-public-data.thelook_ecommerce.orders` o "
                        "CROSS JOIN `bigquery-public-data.thelook_ecommerce.users` u LIMIT 5"
                    ),
                    expected_error="join condition",
                ),
            ),
            _case(
                name="keyless_join_blocked",
                inputs=GuardrailEvalCase(
                    name="keyless_join_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT o.order_id FROM `bigquery-public-data.thelook_ecommerce.orders` o "
                        "JOIN `bigquery-public-data.thelook_ecommerce.users` u LIMIT 5"
                    ),
                    expected_error="join condition",
                ),
            ),
            _case(
                name="unsafe_division_blocked",
                inputs=GuardrailEvalCase(
                    name="unsafe_division_blocked",
                    kind="sql_blocked",
                    sql=(
                        "SELECT SUM(sale_price) / COUNTIF(status = 'Returned') AS ratio "
                        "FROM `bigquery-public-data.thelook_ecommerce.order_items` LIMIT 5"
                    ),
                    expected_error="SAFE_DIVIDE",
                ),
            ),
            _case(
                name="safe_divide_allowed",
                inputs=GuardrailEvalCase(
                    name="safe_divide_allowed",
                    kind="sql_allowed",
                    sql=(
                        "SELECT SAFE_DIVIDE(SUM(sale_price), COUNT(*)) AS average_value "
                        "FROM `bigquery-public-data.thelook_ecommerce.order_items` LIMIT 5"
                    ),
                ),
            ),
            _case(
                name="literal_division_allowed",
                inputs=GuardrailEvalCase(
                    name="literal_division_allowed",
                    kind="sql_allowed",
                    sql=(
                        "SELECT sale_price / 100 AS scaled_value "
                        "FROM `bigquery-public-data.thelook_ecommerce.order_items` LIMIT 5"
                    ),
                ),
            ),
            _case(
                name="alternate_pii_output_redacted",
                inputs=GuardrailEvalCase(
                    name="alternate_pii_output_redacted",
                    kind="redaction",
                    text="Email analyst+retail@example.co.uk or call (415) 555-0199",
                ),
            ),
        ],
        evaluators=[ExpectedPass()],
    )


def _case(name: str, inputs: GuardrailEvalCase) -> Case[GuardrailEvalCase, EvalResult, None]:
    return Case(name=name, inputs=inputs, expected_output=EvalResult(name, True, ""))


def _run_guardrail_case(config: AgentConfig, case: GuardrailEvalCase) -> EvalResult:
    if case.kind == "sql_allowed":
        return _eval_safe_sql(config, case)
    if case.kind == "sql_blocked":
        return _eval_sql_blocked(config, case)
    if case.kind == "redaction":
        return _eval_pii_redaction(case)
    if case.kind == "limit_added":
        return _eval_limit_added(config, case)
    return EvalResult(case.name, False, f"Unknown eval kind: {case.kind}")


def _eval_safe_sql(config: AgentConfig, case: GuardrailEvalCase) -> EvalResult:
    try:
        validate_and_prepare_sql(case.sql, config)
    except Exception as exc:
        return EvalResult(case.name, False, str(exc))
    return EvalResult(case.name, True, "safe aggregate SQL accepted")


def _eval_sql_blocked(config: AgentConfig, case: GuardrailEvalCase) -> EvalResult:
    try:
        validate_and_prepare_sql(case.sql, config)
    except SQLSafetyError as exc:
        passed = case.expected_error in str(exc)
        detail = str(exc).splitlines()[0]
        return EvalResult(case.name, passed, detail)
    return EvalResult(case.name, False, "SQL was not blocked")


def _eval_pii_redaction(case: GuardrailEvalCase) -> EvalResult:
    text, redactions = redact_text(case.text)
    passed = "[REDACTED_EMAIL]" in text and "[REDACTED_PHONE]" in text and redactions == 2
    return EvalResult(case.name, passed, text)


def _eval_limit_added(config: AgentConfig, case: GuardrailEvalCase) -> EvalResult:
    validation = validate_and_prepare_sql(case.sql, config)
    passed = f"LIMIT {config.bigquery.max_result_rows}" in validation.safe_sql
    return EvalResult(case.name, passed, validation.safe_sql)
