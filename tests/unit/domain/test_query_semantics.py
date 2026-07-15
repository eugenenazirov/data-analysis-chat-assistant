from __future__ import annotations

from datetime import date

import pytest

from retail_agent.domain.policies.query_semantics import (
    QuerySemanticError,
    validate_query_semantics,
)


def test_rejects_wrong_timestamp_for_order_item_creation() -> None:
    with pytest.raises(QuerySemanticError, match="order_items.created_at"):
        validate_query_semantics(
            """
            SELECT COUNT(*) AS items
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o
              ON oi.order_id = o.order_id
            WHERE DATE(o.created_at) >= DATE_SUB(
              DATE_TRUNC(CURRENT_DATE(), QUARTER), INTERVAL 1 QUARTER
            )
            """,
            question="Compare order items created last quarter.",
        )


def test_accepts_order_item_timestamp_for_order_item_creation() -> None:
    validate_query_semantics(
        """
        SELECT COUNT(*) AS items
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        WHERE DATE(oi.created_at) >= DATE_SUB(
          DATE_TRUNC(CURRENT_DATE(), QUARTER), INTERVAL 1 QUARTER
        )
        """,
        question="Compare order items created last quarter.",
    )


def test_preserves_prior_dynamic_follow_up_cohort() -> None:
    prior_sql = """
        SELECT COUNT(*) AS items
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        WHERE DATE(oi.created_at) >= DATE_SUB(
          DATE_TRUNC(CURRENT_DATE(), QUARTER), INTERVAL 1 QUARTER
        )
    """

    with pytest.raises(QuerySemanticError, match="dynamic relative-date bounds"):
        validate_query_semantics(
            """
            SELECT SUM(oi.sale_price) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            WHERE DATE(oi.created_at) >= DATE '2026-04-01'
            """,
            question="Which one lost more revenue within that same cohort?",
            prior_sql=prior_sql,
        )


def test_fixed_user_date_does_not_require_current_date() -> None:
    validate_query_semantics(
        """
        SELECT COUNT(*) AS items
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE '2026-01-01'
        """,
        question="How many order items were created after 2026-01-01?",
    )


def test_natural_language_reference_date_allows_fixed_bounds() -> None:
    validate_query_semantics(
        """
        SELECT SUM(sale_price) AS sales
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE '2025-12-01'
          AND DATE(created_at) < DATE '2026-01-01'
        """,
        question=(
            "Using January 15, 2026 as the reference, report the prior month's sales."
        ),
    )


def test_runtime_reference_date_rejects_nondeterministic_current_date() -> None:
    with pytest.raises(QuerySemanticError, match="deterministic half-open date bounds"):
        validate_query_semantics(
            """
            SELECT SUM(sale_price) AS sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH)
            """,
            question="What were sales last complete month?",
            reference_date=date(2026, 7, 13),
        )


def test_runtime_reference_date_accepts_resolved_literal_bounds() -> None:
    validate_query_semantics(
        """
        SELECT SUM(sale_price) AS sales
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE '2026-06-01'
          AND DATE(created_at) < DATE '2026-07-01'
        """,
        question="What were sales last complete month?",
        reference_date=date(2026, 7, 13),
    )


def test_realized_metric_requires_both_status_exclusions() -> None:
    with pytest.raises(QuerySemanticError, match="both 'Cancelled' and 'Returned'"):
        validate_query_semantics(
            """
            SELECT SUM(sale_price) AS realized_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status != 'Returned'
            """,
            question="Report realized sales.",
        )

    validate_query_semantics(
        """
        SELECT SUM(sale_price) AS realized_sales
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE status NOT IN ('Cancelled', 'Returned')
        """,
        question="Report realized sales.",
    )


def test_ungrouped_realized_total_rejects_unrequested_time_series() -> None:
    with pytest.raises(QuerySemanticError, match="one realized-sales total"):
        validate_query_semantics(
            """
            SELECT DATE_TRUNC(DATE(created_at), MONTH) AS sales_month,
                   SUM(sale_price) AS realized_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
            GROUP BY sales_month
            """,
            question=(
                "Report realized sales while consistently excluding cancelled and "
                "returned items."
            ),
        )


def test_single_realized_total_rejects_unrequested_additional_measures() -> None:
    with pytest.raises(QuerySemanticError, match="additional measures"):
        validate_query_semantics(
            """
            SELECT SUM(sale_price) AS realized_sales,
                   COUNT(DISTINCT order_id) AS realized_orders
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
            """,
            question="Report realized sales.",
        )


def test_explicit_realized_time_series_accepts_requested_grouping() -> None:
    validate_query_semantics(
        """
        SELECT DATE_TRUNC(DATE(created_at), MONTH) AS sales_month,
               SUM(sale_price) AS realized_sales
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE status NOT IN ('Cancelled', 'Returned')
        GROUP BY sales_month
        """,
        question="Report the monthly realized sales trend.",
    )


def test_ungrouped_realized_total_allows_grouping_inside_summed_subquery() -> None:
    validate_query_semantics(
        """
        SELECT SUM(category_sales) AS realized_sales
        FROM (
          SELECT category, SUM(sale_price) AS category_sales
          FROM `bigquery-public-data.thelook_ecommerce.order_items`
          WHERE status NOT IN ('Cancelled', 'Returned')
          GROUP BY category
        )
        """,
        question="Report realized sales.",
    )


def test_product_name_grain_rejects_product_identifier_grouping() -> None:
    with pytest.raises(QuerySemanticError, match="requested entity grain is product name"):
        validate_query_semantics(
            """
            SELECT p.name AS product_name, SUM(oi.sale_price) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
              ON oi.product_id = p.id
            GROUP BY p.id, p.name
            """,
            question="Show the top three product names by revenue.",
        )


def test_product_name_grain_accepts_name_only_grouping() -> None:
    validate_query_semantics(
        """
        SELECT p.name AS product_name, SUM(oi.sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        GROUP BY p.name
        """,
        question="Show revenue by product name.",
    )


def test_completed_order_metric_requires_exact_complete_status() -> None:
    broad_realized_sql = """
        SELECT user_id, COUNT(DISTINCT order_id) AS completed_orders
        FROM `bigquery-public-data.thelook_ecommerce.orders`
        WHERE status NOT IN ('Cancelled', 'Returned')
        GROUP BY user_id
    """

    with pytest.raises(QuerySemanticError, match="status is exactly 'Complete'"):
        validate_query_semantics(
            broad_realized_sql,
            question="List customers with more than one completed order.",
        )

    validate_query_semantics(
        broad_realized_sql.replace(
            "status NOT IN ('Cancelled', 'Returned')",
            "status = 'Complete'",
        ),
        question="List customers with more than one completed order.",
    )


def test_completed_order_conditional_count_is_accepted() -> None:
    validate_query_semantics(
        """
        SELECT user_id,
               COUNT(DISTINCT IF(status = 'Complete', order_id, NULL)) AS completed_orders
        FROM `bigquery-public-data.thelook_ecommerce.orders`
        GROUP BY user_id
        HAVING completed_orders > 1
        """,
        question="List customers with more than one completed order.",
    )


def test_top_product_names_require_deterministic_name_tiebreak() -> None:
    sql = """
        SELECT p.name AS product_name, SUM(oi.sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        GROUP BY p.name
        QUALIFY ROW_NUMBER() OVER (ORDER BY SUM(oi.sale_price) DESC) <= 3
    """

    with pytest.raises(QuerySemanticError, match="deterministic secondary product-name"):
        validate_query_semantics(
            sql,
            question="Show the top three product names by revenue.",
        )

    validate_query_semantics(
        sql.replace(
            "ORDER BY SUM(oi.sale_price) DESC",
            "ORDER BY SUM(oi.sale_price) DESC, p.name",
        ),
        question="Show the top three product names by revenue.",
    )


def test_tie_preserving_product_rank_keeps_name_outside_dense_rank() -> None:
    valid_sql = """
        SELECT p.name AS product_name, SUM(oi.sale_price) AS revenue,
               DENSE_RANK() OVER (ORDER BY SUM(oi.sale_price) DESC) AS revenue_rank
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        WHERE oi.status NOT IN ('Cancelled', 'Returned')
        GROUP BY p.name
        QUALIFY DENSE_RANK() OVER (ORDER BY SUM(oi.sale_price) DESC) <= 20
        ORDER BY revenue_rank, product_name
    """

    validate_query_semantics(
        valid_sql,
        question=(
            "Return the top 20 product names by realized revenue, using dense rank "
            "to preserve equal-value ties."
        ),
    )

    with pytest.raises(QuerySemanticError, match="breaks equal-value ties"):
        validate_query_semantics(
            valid_sql.replace(
                "ORDER BY SUM(oi.sale_price) DESC)",
                "ORDER BY SUM(oi.sale_price) DESC, p.name)",
            ),
            question=(
                "Return the top 20 product names by realized revenue, using dense rank "
                "to preserve equal-value ties."
            ),
        )


def test_tie_preserving_product_rank_requires_final_deterministic_sort() -> None:
    sql = """
        SELECT p.name AS product_name, SUM(oi.sale_price) AS revenue,
               DENSE_RANK() OVER (ORDER BY SUM(oi.sale_price) DESC) AS revenue_rank
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        WHERE oi.status NOT IN ('Cancelled', 'Returned')
        GROUP BY p.name
        QUALIFY DENSE_RANK() OVER (ORDER BY SUM(oi.sale_price) DESC) <= 20
        ORDER BY revenue_rank
    """

    with pytest.raises(QuerySemanticError, match="outside the DENSE_RANK window"):
        validate_query_semantics(
            sql,
            question=(
                "Return the top 20 product names by realized revenue, using dense rank "
                "to preserve equal-value ties."
            ),
        )
