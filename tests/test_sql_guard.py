import pytest

from retail_agent.config import load_config
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u FROM `bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 5",
        "SELECT TO_JSON_STRING(u) FROM `bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 5",
        "SELECT ARRAY_AGG(u) FROM `bigquery-public-data.thelook_ecommerce.users` AS u LIMIT 5",
    ],
)
def test_validate_blocks_row_projection_from_pii_table_alias(test_config, sql):
    with pytest.raises(SQLSafetyError, match="row projection"):
        validate_and_prepare_sql(sql, test_config)


@pytest.mark.parametrize(
    "column",
    [
        "first_name",
        "last_name",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "user_geom",
    ],
)
def test_validate_blocks_configured_user_pii_columns(column):
    config = load_config()

    with pytest.raises(SQLSafetyError, match="PII"):
        validate_and_prepare_sql(
            f"SELECT {column} FROM `bigquery-public-data.thelook_ecommerce.users` LIMIT 5",
            config,
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


def test_validate_blocks_excessive_existing_limit(test_config):
    with pytest.raises(SQLSafetyError, match="exceeds maximum"):
        validate_and_prepare_sql(
            """
            SELECT order_id
            FROM `bigquery-public-data.thelook_ecommerce.orders`
            LIMIT 1000000
            """,
            test_config,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders LIMIT 5",
        "SELECT id FROM `thelook_ecommerce.orders` LIMIT 5",
        "SELECT id FROM `other-project.thelook_ecommerce.orders` LIMIT 5",
        "SELECT id FROM `bigquery-public-data.other_dataset.orders` LIMIT 5",
        "SELECT id FROM `example-project.private.orders` LIMIT 5",
    ],
)
def test_validate_blocks_tables_outside_allowed_dataset(test_config, sql):
    with pytest.raises(SQLSafetyError, match="disallowed tables"):
        validate_and_prepare_sql(sql, test_config)


def test_validate_wraps_malformed_sql_as_safety_error(test_config):
    with pytest.raises(SQLSafetyError, match="SQL parse failed"):
        validate_and_prepare_sql("SELECT FROM", test_config)
