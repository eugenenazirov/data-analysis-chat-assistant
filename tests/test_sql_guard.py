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


@pytest.mark.parametrize(
    ("table_name", "dimension", "aggregate_alias"),
    [
        ("orders", "status", "orders"),
        ("users", "gender", "users"),
        ("products", "category", "products"),
    ],
)
def test_validate_allows_output_alias_matching_table_name(
    test_config, table_name, dimension, aggregate_alias
):
    validation = validate_and_prepare_sql(
        f"""
        SELECT {dimension}, COUNT(*) AS {aggregate_alias}
        FROM `bigquery-public-data.thelook_ecommerce.{table_name}`
        GROUP BY {dimension}
        ORDER BY {aggregate_alias} DESC
        LIMIT 5
        """,
        test_config,
    )

    assert "LIMIT 5" in validation.safe_sql
    assert validation.tables == [table_name]


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


def test_validate_preserves_query_without_fabricated_limit(test_config):
    validation = validate_and_prepare_sql(
        """
        SELECT product_id, COUNT(*) AS item_count
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        GROUP BY product_id
        ORDER BY item_count DESC
        """,
        test_config,
    )

    assert "LIMIT" not in validation.safe_sql.upper()


def test_validate_normalizes_timestamp_column_against_calendar_date_bounds(test_config):
    validation = validate_and_prepare_sql(
        """
        SELECT COUNT(*) AS item_count
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        WHERE oi.created_at >= DATE_SUB(
          DATE_TRUNC(CURRENT_DATE(), QUARTER), INTERVAL 1 QUARTER
        )
          AND oi.created_at < DATE_TRUNC(CURRENT_DATE(), QUARTER)
        """,
        test_config,
    )

    assert validation.safe_sql.count("DATE(oi.created_at)") == 2
    assert "INTERVAL 1 QUARTER" in validation.safe_sql


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


@pytest.mark.parametrize(
    "join_sql",
    [
        "CROSS JOIN `bigquery-public-data.thelook_ecommerce.users` AS u",
        "JOIN `bigquery-public-data.thelook_ecommerce.users` AS u",
    ],
)
def test_validate_blocks_cartesian_or_keyless_joins(test_config, join_sql):
    with pytest.raises(SQLSafetyError, match="join condition"):
        validate_and_prepare_sql(
            f"""
            SELECT o.order_id
            FROM `bigquery-public-data.thelook_ecommerce.orders` AS o
            {join_sql}
            LIMIT 5
            """,
            test_config,
        )


@pytest.mark.parametrize(
    "expression",
    [
        "sale_price / id",
        "SUM(sale_price) / COUNTIF(status = 'Returned')",
    ],
)
def test_validate_blocks_unguarded_division(test_config, expression):
    with pytest.raises(SQLSafetyError, match="SAFE_DIVIDE"):
        validate_and_prepare_sql(
            f"""
            SELECT {expression} AS ratio
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            LIMIT 5
            """,
            test_config,
        )


@pytest.mark.parametrize(
    "expression",
    [
        "SAFE_DIVIDE(SUM(sale_price), COUNTIF(status = 'Returned'))",
        "SUM(sale_price) / NULLIF(COUNTIF(status = 'Returned'), 0)",
    ],
)
def test_validate_allows_guarded_division(test_config, expression):
    validation = validate_and_prepare_sql(
        f"""
        SELECT {expression} AS ratio
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        LIMIT 5
        """,
        test_config,
    )

    assert "LIMIT 5" in validation.safe_sql
