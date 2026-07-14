# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evals.quality import QualityReplay, _fixture_content_sha256, _result_schema

DATASET = "bigquery-public-data.thelook_ecommerce"
CAPTURED_AT = "2026-07-14T15:00:00Z"
REFERENCE_DATE = "2026-07-13"
FORBIDDEN_CAUSAL_CLAIMS = [r"\bcaused by\b", r"\bbecause of\b"]
ALLOWED_JOINS = [
    {"left": "order_items.product_id", "right": "products.id"},
    {"left": "order_items.order_id", "right": "orders.order_id"},
    {"left": "order_items.user_id", "right": "users.id"},
    {"left": "order_items.user_id", "right": "customer_orders.user_id"},
    {"left": "orders.user_id", "right": "users.id"},
]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    category: str
    question: str
    sql: str
    rows: list[dict[str, Any]]
    keys: list[str]
    units: dict[str, str]
    answer: str
    risk: Literal["low", "medium", "high", "critical"] = "medium"
    critical: bool = False
    expected_behavior: Literal["answer", "clarify", "refuse", "degrade"] = "answer"
    history: list[str] = field(default_factory=list)
    history_turns: list[dict[str, Any]] = field(default_factory=list)
    conversation_contract: dict[str, Any] | None = None


def _sql(value: str) -> str:
    return " ".join(value.split())


HOLDOUT_SCENARIOS = [
    Scenario(
        "last_complete_month_net_sales",
        "Last complete month versus trailing days",
        "temporal_aggregation",
        "What were net sales in the last fully completed calendar month?",
        _sql("""
            SELECT ROUND(SUM(sale_price), 2) AS net_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
              AND DATE(created_at) >= DATE_TRUNC(DATE_SUB(DATE '2026-07-13', INTERVAL 1 MONTH), MONTH)
              AND DATE(created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
        """),
        [{"net_sales": 125000.0}],
        [],
        {"net_sales": "currency"},
        "Net sales were $125,000 in the last complete month.",
        "critical",
        True,
    ),
    Scenario(
        "quarter_over_quarter_state_growth",
        "Quarter-over-quarter state growth",
        "temporal_aggregation",
        "Compare the two most recent complete quarters by customer state.",
        _sql("""
            SELECT u.state, ROUND(SUM(oi.sale_price), 2) AS current_revenue,
                   ROUND(SAFE_DIVIDE(SUM(oi.sale_price) - 100000, 100000), 3) AS growth_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', QUARTER), INTERVAL 1 QUARTER)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', QUARTER)
            GROUP BY u.state ORDER BY growth_rate DESC, u.state LIMIT 20
        """),
        [
            {"state": "California", "current_revenue": 112000.0, "growth_rate": 0.12},
            {"state": "New York", "current_revenue": 105000.0, "growth_rate": 0.05},
        ],
        ["state"],
        {"state": "text", "current_revenue": "currency", "growth_rate": "percentage"},
        "California's quarter-over-quarter growth rate was 12%.",
        "high",
        True,
    ),
    Scenario(
        "january_previous_month_boundary",
        "January year-boundary month resolution",
        "temporal_aggregation",
        "Using January 15, 2026 as the reference, report the prior month's realized sales.",
        _sql("""
            SELECT ROUND(SUM(sale_price), 2) AS realized_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
              AND DATE(created_at) >= DATE '2025-12-01' AND DATE(created_at) < DATE '2026-01-01'
        """),
        [{"realized_sales": 98000.0}],
        [],
        {"realized_sales": "currency"},
        "December realized sales were $98,000.",
        "high",
        True,
    ),
    Scenario(
        "six_month_category_trend",
        "Six complete months category trend",
        "temporal_aggregation",
        "Show six complete months of Outerwear revenue in chronological order.",
        _sql("""
            SELECT DATE_TRUNC(DATE(oi.created_at), MONTH) AS month,
                   ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE p.category = 'Outerwear & Coats' AND oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', MONTH), INTERVAL 6 MONTH)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
            GROUP BY month ORDER BY month
        """),
        [
            {"month": "2026-01-01", "revenue": 18000.0},
            {"month": "2026-02-01", "revenue": 19000.0},
            {"month": "2026-03-01", "revenue": 20500.0},
            {"month": "2026-04-01", "revenue": 21000.0},
            {"month": "2026-05-01", "revenue": 22500.0},
            {"month": "2026-06-01", "revenue": 24000.0},
        ],
        ["month"],
        {"month": "date", "revenue": "currency"},
        "The six complete-month series is attached in chronological order.",
    ),
    Scenario(
        "fixed_cohort_period_comparison",
        "Fixed state cohort across periods",
        "temporal_aggregation",
        "For California customers only, compare June realized sales with May.",
        _sql("""
            SELECT DATE_TRUNC(DATE(oi.created_at), MONTH) AS month,
                   ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE u.state = 'California' AND oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE '2026-05-01' AND DATE(oi.created_at) < DATE '2026-07-01'
            GROUP BY month ORDER BY month
        """),
        [
            {"month": "2026-05-01", "revenue": 100000.0},
            {"month": "2026-06-01", "revenue": 110000.0},
        ],
        ["month"],
        {"month": "date", "revenue": "currency"},
        "California generated $110,000 in June versus $100,000 in May.",
        "high",
    ),
    Scenario(
        "net_revenue_after_returns",
        "Net revenue after returns",
        "business_semantics",
        "Which categories retain the most revenue after returns are removed?",
        _sql("""
            SELECT p.category, ROUND(SUM(CASE WHEN oi.status NOT IN ('Cancelled', 'Returned') THEN oi.sale_price ELSE 0 END), 2) AS net_revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            GROUP BY p.category ORDER BY net_revenue DESC LIMIT 10
        """),
        [{"category": "Outerwear & Coats", "net_revenue": 90000.0}],
        ["category"],
        {"category": "text", "net_revenue": "currency"},
        "Outerwear & Coats retained $90,000 after returns.",
        "high",
        True,
    ),
    Scenario(
        "average_order_value_distinct_orders",
        "Average order value uses distinct orders",
        "business_semantics",
        "Calculate realized average order value, accounting for multi-item orders.",
        _sql("""
            SELECT ROUND(SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)), 2) AS average_order_spend
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
        """),
        [{"average_order_spend": 85.5}],
        [],
        {"average_order_spend": "currency"},
        "Average realized spend per distinct order was $85.50.",
        "critical",
        True,
    ),
    Scenario(
        "unit_return_rate_by_category",
        "Unit-based return rate",
        "business_semantics",
        "Rank categories by the share of sold units that were returned.",
        _sql("""
            SELECT p.category, ROUND(SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'), COUNT(*)), 3) AS unit_return_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            GROUP BY p.category ORDER BY unit_return_rate DESC LIMIT 10
        """),
        [{"category": "Outerwear & Coats", "unit_return_rate": 0.08}],
        ["category"],
        {"category": "text", "unit_return_rate": "percentage"},
        "Outerwear & Coats had an 8% unit return rate.",
    ),
    Scenario(
        "returned_revenue_rate_by_category",
        "Returned-revenue rate",
        "business_semantics",
        "Rank categories by returned sales value as a share of gross sales value.",
        _sql("""
            SELECT p.category, ROUND(SAFE_DIVIDE(SUM(IF(oi.status = 'Returned', oi.sale_price, 0)), SUM(oi.sale_price)), 3) AS returned_revenue_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            GROUP BY p.category ORDER BY returned_revenue_rate DESC LIMIT 10
        """),
        [{"category": "Outerwear & Coats", "returned_revenue_rate": 0.11}],
        ["category"],
        {"category": "text", "returned_revenue_rate": "percentage"},
        "Outerwear & Coats had an 11% returned-revenue rate.",
    ),
    Scenario(
        "minimum_sample_return_rate",
        "Return rate with minimum sample",
        "business_semantics",
        "Show categories with at least 100 sold items and the highest return rate.",
        _sql("""
            SELECT p.category, COUNT(*) AS items_sold, ROUND(SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'), COUNT(*)), 3) AS return_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            GROUP BY p.category HAVING COUNT(*) >= 100 ORDER BY return_rate DESC LIMIT 10
        """),
        [{"category": "Outerwear & Coats", "items_sold": 150, "return_rate": 0.09}],
        ["category"],
        {"category": "text", "items_sold": "count", "return_rate": "percentage"},
        "Outerwear & Coats met the threshold with 150 items and a 9% return rate.",
        "high",
    ),
]

HOLDOUT_SCENARIOS += [
    Scenario(
        "top_three_products_per_category",
        "Top three products within each category",
        "ranking_distribution",
        "Return the top three products inside every category by realized revenue.",
        _sql("""
            SELECT p.category, p.name AS product_name, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY p.category, p.name
            QUALIFY ROW_NUMBER() OVER (PARTITION BY p.category ORDER BY SUM(oi.sale_price) DESC, p.name) <= 3
            ORDER BY p.category, revenue DESC, product_name
        """),
        [
            {"category": "Outerwear & Coats", "product_name": "Jacket", "revenue": 50000.0},
            {"category": "Outerwear & Coats", "product_name": "Coat", "revenue": 45000.0},
            {"category": "Outerwear & Coats", "product_name": "Parka", "revenue": 40000.0},
        ],
        ["category", "product_name"],
        {"category": "text", "product_name": "text", "revenue": "currency"},
        "Jacket led Outerwear & Coats with $50,000 in realized revenue.",
        "critical",
        True,
    ),
    Scenario(
        "state_share_of_realized_revenue",
        "State share of total revenue",
        "ranking_distribution",
        "What percentage of realized revenue came from each customer state?",
        _sql("""
            SELECT u.state, ROUND(SAFE_DIVIDE(SUM(oi.sale_price), SUM(SUM(oi.sale_price)) OVER ()), 3) AS revenue_share_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY u.state ORDER BY revenue_share_rate DESC, u.state LIMIT 20
        """),
        [
            {"state": "California", "revenue_share_rate": 0.55},
            {"state": "New York", "revenue_share_rate": 0.45},
        ],
        ["state"],
        {"state": "text", "revenue_share_rate": "percentage"},
        "California contributed 55% of realized revenue.",
        "high",
        True,
    ),
    Scenario(
        "equal_revenue_tie_handling",
        "Explicit equal-rank handling",
        "ranking_distribution",
        "Rank products by realized revenue and preserve equal first-place ties.",
        _sql("""
            SELECT p.name AS product_name, ROUND(SUM(oi.sale_price), 2) AS revenue,
                   DENSE_RANK() OVER (ORDER BY SUM(oi.sale_price) DESC) AS revenue_rank
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY p.name ORDER BY revenue_rank, product_name LIMIT 20
        """),
        [
            {"product_name": "Coat", "revenue": 50000.0, "revenue_rank": 1},
            {"product_name": "Jacket", "revenue": 50000.0, "revenue_rank": 1},
        ],
        ["product_name"],
        {"product_name": "text", "revenue": "currency", "revenue_rank": "count"},
        "Revenue was $50,000 for both Coat and Jacket, so they tied for first.",
    ),
    Scenario(
        "deterministic_empty_product_cohort",
        "Empty result without invention",
        "ranking_distribution",
        "Summarize sales for product ID -1, which is absent from the catalog.",
        _sql("""
            SELECT product_id, ROUND(SUM(sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE product_id = -1 GROUP BY product_id ORDER BY product_id
        """),
        [],
        ["product_id"],
        {"product_id": "identifier", "revenue": "currency"},
        "No matching data was found for that product cohort.",
    ),
    Scenario(
        "deterministic_secondary_state_sort",
        "Deterministic secondary ranking sort",
        "ranking_distribution",
        "Rank states by realized revenue and sort equal values by state name.",
        _sql("""
            SELECT u.state, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY u.state ORDER BY revenue DESC, u.state LIMIT 20
        """),
        [{"state": "California", "revenue": 120000.0}],
        ["state"],
        {"state": "text", "revenue": "currency"},
        "California ranked first with $120,000 in realized revenue.",
    ),
    Scenario(
        "repeat_customer_ids_only",
        "Repeat customers without direct identifiers",
        "customer_cohorts",
        "List customer IDs with more than one distinct completed order.",
        _sql("""
            SELECT user_id AS customer_id, COUNT(DISTINCT order_id) AS completed_orders
            FROM `bigquery-public-data.thelook_ecommerce.orders`
            WHERE status = 'Complete' GROUP BY user_id HAVING COUNT(DISTINCT order_id) > 1
            ORDER BY completed_orders DESC, customer_id LIMIT 20
        """),
        [{"customer_id": 1001, "completed_orders": 3}],
        ["customer_id"],
        {"customer_id": "identifier", "completed_orders": "count"},
        "Customer ID 1001 had 3 distinct completed orders.",
        "critical",
        True,
    ),
    Scenario(
        "single_vs_repeat_customer_spend",
        "Single-order versus repeat-customer spend",
        "customer_cohorts",
        "Compare average customer spend for one-order customers and repeat customers.",
        _sql("""
            WITH customer_spend AS (
              SELECT o.user_id, COUNT(DISTINCT o.order_id) AS order_count, SUM(oi.sale_price) AS spend
              FROM `bigquery-public-data.thelook_ecommerce.orders` AS o
              JOIN `bigquery-public-data.thelook_ecommerce.order_items` AS oi ON o.order_id = oi.order_id
              WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY o.user_id
            )
            SELECT IF(order_count = 1, 'single_order', 'repeat') AS cohort, ROUND(AVG(spend), 2) AS average_spend
            FROM customer_spend GROUP BY cohort ORDER BY cohort
        """),
        [
            {"cohort": "repeat", "average_spend": 140.0},
            {"cohort": "single_order", "average_spend": 75.0},
        ],
        ["cohort"],
        {"cohort": "text", "average_spend": "currency"},
        "Repeat customers averaged $140 versus $75 for one-order customers.",
        "high",
    ),
    Scenario(
        "new_vs_repeat_revenue_mix",
        "New versus repeat revenue mix",
        "customer_cohorts",
        "Split realized revenue between first-time and repeat customers.",
        _sql("""
            WITH customer_orders AS (
              SELECT user_id, COUNT(DISTINCT order_id) AS order_count
              FROM `bigquery-public-data.thelook_ecommerce.orders` GROUP BY user_id
            )
            SELECT IF(c.order_count = 1, 'new', 'repeat') AS cohort, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN customer_orders AS c ON oi.user_id = c.user_id
            WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY cohort ORDER BY revenue DESC
        """),
        [
            {"cohort": "repeat", "revenue": 80000.0},
            {"cohort": "new", "revenue": 45000.0},
        ],
        ["cohort"],
        {"cohort": "text", "revenue": "currency"},
        "Repeat customers generated $80,000 in realized revenue.",
    ),
    Scenario(
        "traffic_source_average_order_value",
        "Traffic-source order value cohorts",
        "customer_cohorts",
        "Compare realized average order value by customer traffic source.",
        _sql("""
            SELECT u.traffic_source, ROUND(SAFE_DIVIDE(SUM(oi.sale_price), COUNT(DISTINCT oi.order_id)), 2) AS average_order_spend
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY u.traffic_source ORDER BY average_order_spend DESC LIMIT 20
        """),
        [{"traffic_source": "Search", "average_order_spend": 95.0}],
        ["traffic_source"],
        {"traffic_source": "text", "average_order_spend": "currency"},
        "Search customers averaged $95 in spend per realized order.",
    ),
    Scenario(
        "age_band_realized_revenue",
        "Age-band realized revenue",
        "customer_cohorts",
        "Group realized revenue into ten-year customer age bands.",
        _sql("""
            SELECT CONCAT(CAST(DIV(u.age, 10) * 10 AS STRING), '-', CAST(DIV(u.age, 10) * 10 + 9 AS STRING)) AS age_band,
                   ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY age_band ORDER BY revenue DESC, age_band
        """),
        [{"age_band": "30-39", "revenue": 120000.0}],
        ["age_band"],
        {"age_band": "text", "revenue": "currency"},
        "The leading age band generated $120,000 in realized revenue.",
    ),
]

MULTI_TURN_SCENARIOS = [
    Scenario(
        id="state_return_loss_trajectory",
        title="Three-turn state return-loss trajectory",
        category="cohort_preservation",
        question="Within that cohort, what percentage of realized revenue was lost in each state?",
        sql=_sql("""
            SELECT u.state, ROUND(SUM(IF(oi.status = 'Returned', oi.sale_price, 0)), 2) AS return_loss,
                   ROUND(SAFE_DIVIDE(SUM(IF(oi.status = 'Returned', oi.sale_price, 0)), SUM(IF(oi.status NOT IN ('Cancelled', 'Returned'), oi.sale_price, 0))), 3) AS loss_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE u.state IN ('California', 'New York')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', QUARTER), INTERVAL 1 QUARTER)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', QUARTER)
            GROUP BY u.state ORDER BY loss_rate DESC, u.state
        """),
        rows=[
            {"state": "New York", "return_loss": 300.0, "loss_rate": 0.03},
            {"state": "California", "return_loss": 200.0, "loss_rate": 0.02},
        ],
        keys=["state"],
        units={"state": "text", "return_loss": "currency", "loss_rate": "percentage"},
        answer="New York's loss rate was 3% versus California's 2% within the retained cohort.",
        risk="high",
        critical=True,
        history=[
            "Compare California and New York realized revenue last quarter.",
            "Which state lost more revenue to returns within that cohort?",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["u.state in ('california', 'new york')", "interval 1 quarter"],
            "superseded_constraints": [],
            "referenced_entities": ["California", "New York"],
            "effective_period": "date '2026-07-13'",
        },
    ),
    Scenario(
        id="category_threshold_prior_month",
        title="Added threshold and prior-month comparison",
        category="constraint_addition",
        question="Compare the winning category with the month before.",
        sql=_sql("""
            SELECT p.category, DATE_TRUNC(DATE(oi.created_at), MONTH) AS month,
                   COUNT(DISTINCT oi.order_id) AS orders, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', MONTH), INTERVAL 2 MONTH)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
            GROUP BY p.category, month HAVING COUNT(DISTINCT oi.order_id) >= 100
            ORDER BY revenue DESC, p.category, month
        """),
        rows=[
            {"category": "Outerwear & Coats", "month": "2026-06-01", "orders": 120, "revenue": 50000.0},
            {"category": "Outerwear & Coats", "month": "2026-05-01", "orders": 110, "revenue": 45000.0},
        ],
        keys=["category", "month"],
        units={"category": "text", "month": "date", "orders": "count", "revenue": "currency"},
        answer="Outerwear & Coats rose from $45,000 to $50,000 while retaining the minimum-order threshold.",
        history=[
            "Show the top categories last complete month.",
            "Only keep categories with at least 100 distinct orders.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["count(distinct oi.order_id) >= 100", "interval 2 month"],
            "superseded_constraints": [],
            "referenced_entities": ["Outerwear & Coats"],
            "effective_period": "date '2026-07-13'",
        },
    ),
    Scenario(
        id="gross_to_net_user_correction",
        title="Latest correction replaces gross-sales intent",
        category="user_correction",
        question="Now rank categories using the corrected measure.",
        sql=_sql("""
            SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS net_realized_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY p.category ORDER BY net_realized_sales DESC LIMIT 10
        """),
        rows=[{"category": "Outerwear & Coats", "net_realized_sales": 90000.0}],
        keys=["category"],
        units={"category": "text", "net_realized_sales": "currency"},
        answer="Outerwear & Coats led corrected net realized sales with $90,000.",
        risk="high",
        history=[
            "Rank categories by gross sales.",
            "Correction: use net realized sales and exclude cancelled and returned items.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["status not in ('cancelled', 'returned')"],
            "superseded_constraints": ["gross_sales"],
            "referenced_entities": ["Outerwear & Coats"],
        },
    ),
    Scenario(
        id="ambiguous_pronoun_two_dimensions",
        title="Ambiguous pronoun with product and state candidates",
        category="pronoun_resolution",
        question="Which one performed better?",
        sql="SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        rows=[],
        keys=[],
        units={},
        answer="Do you mean which product or which state performed better?",
        expected_behavior="clarify",
        history=[
            "Compare Jacket and Coat revenue.",
            "Also compare California and New York revenue.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": [],
            "superseded_constraints": [],
            "referenced_entities": ["product", "state"],
        },
    ),
    Scenario(
        id="four_constraint_history_retention",
        title="Long history retains four constraints",
        category="long_history",
        question="Run the final comparison with every constraint retained.",
        sql=_sql("""
            SELECT p.category, COUNT(*) AS items, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE u.state = 'New York' AND oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', MONTH), INTERVAL 1 MONTH)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
            GROUP BY p.category HAVING COUNT(*) >= 50 ORDER BY revenue DESC
        """),
        rows=[{"category": "Outerwear & Coats", "items": 75, "revenue": 30000.0}],
        keys=["category"],
        units={"category": "text", "items": "count", "revenue": "currency"},
        answer="Outerwear & Coats led with $30,000 across 75 items under all retained constraints.",
        risk="high",
        history=[
            "Use New York customers.",
            "Use realized statuses only.",
            "Use the last complete month.",
            "Require at least 50 items per category.",
        ],
        history_turns=[{"succeeded": True, "trusted": True}] * 4,
        conversation_contract={
            "retained_constraints": [
                "u.state = 'new york'",
                "status not in ('cancelled', 'returned')",
                "interval 1 month",
                "count(*) >= 50",
            ],
            "superseded_constraints": [],
            "referenced_entities": ["New York"],
            "effective_period": "date '2026-07-13'",
        },
    ),
]

MULTI_TURN_SCENARIOS += [
    Scenario(
        id="topic_switch_clears_category_filter",
        title="Topic switch clears stale category scope",
        category="topic_switch",
        question="How many repeat customers do we have instead?",
        sql=_sql("""
            SELECT COUNT(*) AS repeat_customers FROM (
              SELECT user_id FROM `bigquery-public-data.thelook_ecommerce.orders`
              WHERE status = 'Complete' GROUP BY user_id HAVING COUNT(DISTINCT order_id) > 1
            )
        """),
        rows=[{"repeat_customers": 320}],
        keys=[],
        units={"repeat_customers": "count"},
        answer="There were 320 repeat customers after the topic switch.",
        history=[
            "Show Outerwear revenue last month.",
            "Break that category down by product.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["count(distinct order_id) > 1"],
            "superseded_constraints": ["p.category", "outerwear"],
            "referenced_entities": ["repeat customers"],
        },
    ),
    Scenario(
        id="stable_reference_date_sequence",
        title="Stable reference date across relative periods",
        category="reference_date_consistency",
        question="Compare that period with the month before it.",
        sql=_sql("""
            SELECT DATE_TRUNC(DATE(created_at), MONTH) AS month, ROUND(SUM(sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
              AND DATE(created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', MONTH), INTERVAL 2 MONTH)
              AND DATE(created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
            GROUP BY month ORDER BY month
        """),
        rows=[
            {"month": "2026-05-01", "revenue": 92000.0},
            {"month": "2026-06-01", "revenue": 98000.0},
        ],
        keys=["month"],
        units={"month": "date", "revenue": "currency"},
        answer="Revenue rose from $92,000 in May to $98,000 in June under one reference date.",
        risk="high",
        history=[
            "What were sales last month?",
            "Keep that period as our comparison anchor.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["interval 2 month"],
            "superseded_constraints": [],
            "referenced_entities": ["May", "June"],
            "effective_period": "date '2026-07-13'",
        },
    ),
    Scenario(
        id="failed_turn_recovery_lineage",
        title="Failed turn is excluded from trusted lineage",
        category="failed_turn_recovery",
        question="Continue with the corrected result and show the category leader.",
        sql=_sql("""
            SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY p.category ORDER BY revenue DESC LIMIT 10
        """),
        rows=[{"category": "Outerwear & Coats", "revenue": 90000.0}],
        keys=["category"],
        units={"category": "text", "revenue": "currency"},
        answer="Outerwear & Coats led the corrected result with $90,000.",
        risk="critical",
        critical=True,
        history=[
            "Run category revenue; the warehouse error is injected for this turn.",
            "Retry with safe realized-sales logic.",
        ],
        history_turns=[
            {"succeeded": False, "trusted": False},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["status not in ('cancelled', 'returned')"],
            "superseded_constraints": [],
            "referenced_entities": ["Outerwear & Coats"],
        },
    ),
    Scenario(
        id="clear_conversation_removes_references",
        title="Clear conversation removes stale references",
        category="conversation_reset",
        question="Compare it with before.",
        sql="SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        rows=[],
        keys=[],
        units={},
        answer="The conversation was cleared. What should 'it' and 'before' refer to?",
        expected_behavior="clarify",
        history=["Clear the current conversation and its retained analysis context."],
        history_turns=[{"succeeded": True, "trusted": True}],
        conversation_contract={
            "expect_history_used": False,
            "retained_constraints": [],
            "superseded_constraints": ["outerwear", "california"],
            "referenced_entities": ["conversation was cleared"],
        },
    ),
    Scenario(
        id="same_cohort_different_complete_month",
        title="Same cohort in another complete month",
        category="cohort_preservation",
        question="Now compare the identical cohort in the prior complete month.",
        sql=_sql("""
            SELECT DATE_TRUNC(DATE(oi.created_at), MONTH) AS month, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
            WHERE u.state = 'California' AND oi.status NOT IN ('Cancelled', 'Returned')
              AND DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(DATE '2026-07-13', MONTH), INTERVAL 2 MONTH)
              AND DATE(oi.created_at) < DATE_TRUNC(DATE '2026-07-13', MONTH)
            GROUP BY month ORDER BY month
        """),
        rows=[
            {"month": "2026-05-01", "revenue": 100000.0},
            {"month": "2026-06-01", "revenue": 110000.0},
        ],
        keys=["month"],
        units={"month": "date", "revenue": "currency"},
        answer="The same California cohort rose from $100,000 to $110,000.",
        risk="high",
        history=[
            "Use California customers with realized sales only.",
            "Show their last complete month revenue.",
        ],
        history_turns=[
            {"succeeded": True, "trusted": True},
            {"succeeded": True, "trusted": True},
        ],
        conversation_contract={
            "retained_constraints": ["u.state = 'california'", "status not in ('cancelled', 'returned')"],
            "superseded_constraints": [],
            "referenced_entities": ["California"],
            "effective_period": "date '2026-07-13'",
        },
    ),
]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_tables(sql: str) -> list[str]:
    return sorted(set(re.findall(r"`([^`]+)`", sql)))


def _case_from_scenario(scenario: Scenario, suite: str) -> dict[str, Any]:
    answer_case = scenario.expected_behavior in {"answer", "degrade"}
    candidate_sql = scenario.sql if answer_case else ""
    report_sql = candidate_sql or None
    report = {
        "question": scenario.question,
        "answer": scenario.answer,
        "sql": report_sql,
        "refused": scenario.expected_behavior == "refuse",
        "degraded": scenario.expected_behavior == "degrade",
    }
    unit_types = {
        "currency": "number",
        "percentage": "number",
        "count": "integer",
        "identifier": "integer",
        "text": "string",
        "date": "string",
    }
    result_schema = _result_schema(scenario.rows) or {
        column: unit_types[unit] for column, unit in scenario.units.items()
    }
    source_tables = _source_tables(scenario.sql)
    table_names = sorted({table.rsplit(".", 1)[-1] for table in source_tables})
    replay: dict[str, Any] = {
        "candidate_sql": candidate_sql,
        "candidate_rows": scenario.rows if answer_case else [],
        "canonical_rows": scenario.rows if answer_case else [],
        "retrieved_ids": [],
        "report": report,
        "history_used": (
            scenario.conversation_contract or {}
        ).get("expect_history_used", True) if scenario.history else False,
        "history_turns": [
            {"question": question, "sql": None, **turn}
            for question, turn in zip(
                scenario.history, scenario.history_turns, strict=True
            )
        ],
        "usefulness_score": None,
        "reference_date": REFERENCE_DATE,
        "provenance": {
            "canonical_sql_sha256": _sha256_text(scenario.sql),
            "reference_date": REFERENCE_DATE,
            "bigquery_location": "US",
            "source_datasets": sorted({table.rsplit(".", 1)[0] for table in source_tables}),
            "source_tables": source_tables,
            "result_schema": result_schema,
            "row_count": len(scenario.rows) if answer_case else 0,
            "captured_at": CAPTURED_AT,
            "evaluator_version": "quality-v2",
            "prompt_version": "analysis-v3",
            "persona_version": "prototype-config-v1",
            "model": "google-cloud:gemini-2.5-flash",
            "embedding_model": "gemini-embedding-001",
            "golden_index_version": "golden_trios",
            "from_cache": True,
            "content_sha256": "0" * 64,
        },
    }
    replay_model = QualityReplay.model_validate(replay)
    replay["provenance"]["content_sha256"] = _fixture_content_sha256(
        scenario.sql, replay_model
    )
    columns = list(result_schema)
    evaluators = ["faithfulness", "usefulness"]
    if answer_case:
        evaluators = ["intent", "calculation", *evaluators]
    if scenario.history:
        evaluators.append("multi_turn")
    return {
        "id": scenario.id,
        "title": scenario.title,
        "suite": suite,
        "category": scenario.category,
        "risk": scenario.risk,
        "question": scenario.question,
        "user_id": "manager_a",
        "history": scenario.history,
        "reference_date": REFERENCE_DATE,
        "expected_behavior": scenario.expected_behavior,
        "modes": ["replay", "live"],
        "evaluators": evaluators,
        "canonical_sql": scenario.sql,
        "expectations": {
            "required_tables": table_names if answer_case else [],
            "allowed_joins": ALLOWED_JOINS,
            "required_sql_fragments": [],
            "forbidden_sql_fragments": ["select *"],
            "expected_retrieval_ids": [],
            "numeric_tolerance": 0.001,
        },
        "result_contract": {
            "key_columns": scenario.keys,
            "measure_columns": [column for column in columns if column not in scenario.keys],
            "column_mapping": {column: column for column in columns},
            "ordered": True,
            "numeric_tolerance": 0.001,
            "units": scenario.units,
        },
        "answer_contract": {
            "required_facts": [],
            "forbidden_claims": FORBIDDEN_CAUSAL_CLAIMS,
            "pii_forbidden": True,
        },
        "conversation_contract": scenario.conversation_contract,
        "budgets": {
            "max_query_attempts": 1 if answer_case else 0,
            "max_output_retries": 1,
            "max_provider_requests": 4,
            "max_retrieval_requests": 1,
            "max_bigquery_jobs": 2 if answer_case else 0,
            "max_bytes_processed": 50_000_000 if answer_case else 0,
            "max_duration_seconds": 30,
            "max_total_tokens": 16_000,
        },
        "human_rubric": "retail_analysis_v1",
        "replay": replay,
        "critical": scenario.critical,
    }


def _render(scenarios: list[Scenario], suite: str) -> str:
    return "".join(
        json.dumps(_case_from_scenario(scenario, suite), separators=(",", ":")) + "\n"
        for scenario in scenarios
    )


def _write_or_check(path: Path, content: str, *, check: bool) -> None:
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"stale replay fixtures: {path}")
        return
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).parent
    _write_or_check(
        root / "release_holdout.jsonl",
        _render(HOLDOUT_SCENARIOS, "release_holdout"),
        check=args.check,
    )
    _write_or_check(
        root / "multi_turn.jsonl",
        _render(MULTI_TURN_SCENARIOS, "multi_turn"),
        check=args.check,
    )


HOLDOUT_SCENARIOS += [
    Scenario(
        "ambiguous_best_customers",
        "Undefined best-customer metric",
        "limitations_ambiguity",
        "Who are our best customers right now?",
        "SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        [],
        [],
        {},
        "Should best mean realized spend, completed-order count, or another metric?",
        "high",
        True,
        "clarify",
    ),
    Scenario(
        "unavailable_conversion_rate",
        "Unavailable visit conversion denominator",
        "limitations_ambiguity",
        "What is our visitor-to-order conversion rate by traffic source?",
        "SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        [],
        [],
        {},
        "Conversion rate is unavailable because the approved schema has no visits or sessions.",
        "critical",
        True,
        "refuse",
    ),
    Scenario(
        "unavailable_store_branch_dimension",
        "Unavailable store-branch dimension",
        "limitations_ambiguity",
        "Compare physical store branches and identify the weakest location.",
        "SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        [],
        [],
        {},
        "The schema has customer geography but no store branch. Should state be used as an explicit proxy?",
        "high",
        True,
        "clarify",
    ),
    Scenario(
        "return_pattern_without_causal_claim",
        "Return pattern without causal overreach",
        "limitations_ambiguity",
        "Why are returns high, using only evidence available in the retail tables?",
        _sql("""
            SELECT p.category, ROUND(SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'), COUNT(*)), 3) AS return_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            GROUP BY p.category ORDER BY return_rate DESC LIMIT 10
        """),
        [{"category": "Outerwear & Coats", "return_rate": 0.12}],
        ["category"],
        {"category": "text", "return_rate": "percentage"},
        "Outerwear & Coats showed a 12% return rate; these tables support association, not cause.",
        "critical",
        True,
    ),
    Scenario(
        "conflicting_all_and_top_ten",
        "Conflicting all-versus-top-ten scope",
        "limitations_ambiguity",
        "Show all customers, but only return the top ten.",
        "SELECT COUNT(*) AS row_count FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE FALSE",
        [],
        [],
        {},
        "Should the result include every customer or only the ten highest-ranked customers?",
        "high",
        True,
        "clarify",
    ),
    Scenario(
        "zero_denominator_safe_return_rate",
        "Safe zero-denominator handling",
        "data_quality_edge",
        "Calculate the return rate for an absent product cohort without division errors.",
        _sql("""
            SELECT product_id, SAFE_DIVIDE(COUNTIF(status = 'Returned'), COUNT(*)) AS return_rate
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE product_id = -1 GROUP BY product_id ORDER BY product_id
        """),
        [],
        ["product_id"],
        {"product_id": "identifier", "return_rate": "percentage"},
        "No matching data was found, and no rate was fabricated.",
        "high",
    ),
    Scenario(
        "null_category_bucket",
        "Null category is explicit",
        "data_quality_edge",
        "Include products without a category in a clearly named bucket.",
        _sql("""
            SELECT COALESCE(p.category, 'Uncategorized') AS category, ROUND(SUM(oi.sale_price), 2) AS revenue
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id
            WHERE oi.status NOT IN ('Cancelled', 'Returned')
            GROUP BY category ORDER BY revenue DESC LIMIT 20
        """),
        [{"category": "Uncategorized", "revenue": 15000.0}],
        ["category"],
        {"category": "text", "revenue": "currency"},
        "Uncategorized products generated $15,000 in realized revenue.",
    ),
    Scenario(
        "distinct_orders_with_multi_item_rows",
        "Distinct orders across multi-item rows",
        "data_quality_edge",
        "Count completed orders without double-counting their item rows.",
        _sql("""
            SELECT COUNT(DISTINCT order_id) AS completed_orders
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status = 'Complete'
        """),
        [{"completed_orders": 1250}],
        [],
        {"completed_orders": "count"},
        "There were 1,250 distinct completed orders.",
    ),
    Scenario(
        "half_open_daily_boundary",
        "Half-open daily time boundary",
        "data_quality_edge",
        "Count items created on July 1 without leaking in July 2 midnight rows.",
        _sql("""
            SELECT COUNT(*) AS item_count
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE created_at >= TIMESTAMP('2026-07-01') AND created_at < TIMESTAMP('2026-07-02')
        """),
        [{"item_count": 420}],
        [],
        {"item_count": "count"},
        "The requested half-open daily window contained 420 items.",
    ),
    Scenario(
        "consistent_realized_status_policy",
        "Consistent realized-status exclusions",
        "data_quality_edge",
        "Report realized sales while consistently excluding cancelled and returned items.",
        _sql("""
            SELECT ROUND(SUM(sale_price), 2) AS realized_sales
            FROM `bigquery-public-data.thelook_ecommerce.order_items`
            WHERE status NOT IN ('Cancelled', 'Returned')
        """),
        [{"realized_sales": 98000.0}],
        [],
        {"realized_sales": "currency"},
        "Realized sales were $98,000 after consistent status exclusions.",
        "high",
    ),
]


if __name__ == "__main__":
    main()
