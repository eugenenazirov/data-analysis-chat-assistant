from retail_agent.cli import BIGQUERY_SMOKE_SQL
from retail_agent.sql_guard import validate_and_prepare_sql


def test_bigquery_smoke_sql_is_guardrail_safe(test_config):
    validation = validate_and_prepare_sql(BIGQUERY_SMOKE_SQL, test_config)

    assert validation.tables == ["order_items"]
    assert "email" not in validation.safe_sql.lower()
    assert "phone" not in validation.safe_sql.lower()
