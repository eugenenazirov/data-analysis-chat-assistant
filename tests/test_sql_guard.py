import pytest

from retail_agent.sql_guard import SQLSafetyError, validate_and_prepare_sql


def test_validate_allows_safe_aggregate_sql(test_config):
    validation = validate_and_prepare_sql(
        """
        SELECT product_id, COUNT(*) AS item_count
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        GROUP BY product_id
        ORDER BY item_count DESC
        LIMIT 10
        """,
        test_config,
    )

    assert "LIMIT 10" in validation.safe_sql
    assert validation.tables == ["order_items"]


def test_validate_blocks_select_star(test_config):
    with pytest.raises(SQLSafetyError, match=r"SELECT \*"):
        validate_and_prepare_sql(
            "SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
            test_config,
        )


def test_validate_blocks_pii_column(test_config):
    with pytest.raises(SQLSafetyError, match="PII"):
        validate_and_prepare_sql(
            "SELECT email FROM `bigquery-public-data.thelook_ecommerce.users` LIMIT 5",
            test_config,
        )


def test_validate_blocks_destructive_sql(test_config):
    with pytest.raises(SQLSafetyError, match="Only SELECT"):
        validate_and_prepare_sql(
            "DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE order_id = 1",
            test_config,
        )


def test_validate_adds_limit_when_missing(test_config):
    validation = validate_and_prepare_sql(
        """
        SELECT product_id, COUNT(*) AS item_count
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        GROUP BY product_id
        ORDER BY item_count DESC
        """,
        test_config,
    )

    assert "LIMIT 25" in validation.safe_sql
