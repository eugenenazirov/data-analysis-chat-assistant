from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from retail_agent.models import UserProfile

DEFAULT_SQL_RETRIES = 2
DEFAULT_HISTORY_BYTES = 64 * 1024


class BigQueryConfig(BaseModel):
    project: str | None = None
    location: str = "US"
    dataset: str = "bigquery-public-data.thelook_ecommerce"
    allowed_tables: list[str] = Field(
        default_factory=lambda: ["orders", "order_items", "products", "users"]
    )
    max_bytes_billed: int = 200_000_000
    timeout_seconds: int = 60
    max_result_rows: int = 100
    job_label_app: str = "retail-agent"


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    api_key: str | None = None
    collection: str = "golden_trios"
    timeout_seconds: int = 30


class ModelConfig(BaseModel):
    llm_model: str = "google-cloud:gemini-2.5-flash"
    embedding_provider: str = Field(default="gemini", pattern="^(gemini|hash)$")
    embedding_model: str = "gemini-embedding-001"
    max_sql_retries: int = Field(default=DEFAULT_SQL_RETRIES, ge=0, le=3)
    max_history_turns: int = Field(default=6, ge=1, le=20)
    max_history_bytes: int = Field(default=DEFAULT_HISTORY_BYTES, ge=4_096, le=1_048_576)
    temperature: float = 0.1


SAFE_COLUMNS_BY_TABLE: dict[str, list[str]] = {
    "orders": [
        "order_id",
        "user_id",
        "status",
        "gender",
        "created_at",
        "returned_at",
        "shipped_at",
        "delivered_at",
        "num_of_item",
    ],
    "order_items": [
        "id",
        "order_id",
        "user_id",
        "product_id",
        "inventory_item_id",
        "status",
        "created_at",
        "shipped_at",
        "delivered_at",
        "returned_at",
        "sale_price",
    ],
    "products": [
        "id",
        "cost",
        "category",
        "name",
        "brand",
        "retail_price",
        "department",
        "sku",
        "distribution_center_id",
    ],
    "users": [
        "id",
        "age",
        "gender",
        "state",
        "city",
        "country",
        "traffic_source",
        "created_at",
    ],
}


class SafetyConfig(BaseModel):
    pii_columns: list[str] = Field(
        default_factory=lambda: [
            "email",
            "phone",
            "phone_number",
            "customer_email",
            "customer_phone",
            "first_name",
            "last_name",
            "street_address",
            "postal_code",
            "zip",
            "latitude",
            "longitude",
            "user_geom",
        ]
    )
    safe_columns_by_table: dict[str, list[str]] = Field(
        default_factory=lambda: {
            table: list(columns) for table, columns in SAFE_COLUMNS_BY_TABLE.items()
        }
    )
    blocked_sql_keywords: list[str] = Field(
        default_factory=lambda: [
            "alter",
            "create",
            "delete",
            "drop",
            "insert",
            "merge",
            "truncate",
            "update",
        ]
    )


class ObservabilityConfig(BaseModel):
    log_path: Path = Path("logs/agent-runs.jsonl")
    enable_logfire: bool = False


class AgentConfig(BaseModel):
    bigquery: BigQueryConfig = Field(default_factory=BigQueryConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    persona_tone: str = "clear, concise, executive-friendly"
    persona_version: str = "prototype-config-v1"
    users: dict[str, UserProfile] = Field(default_factory=dict)
    golden_trios_path: Path = Path("data/golden_trios.jsonl")

    def user_profile(self, user_id: str) -> UserProfile:
        return self.users.get(
            user_id,
            UserProfile(
                user_id=user_id,
                display_name=user_id,
                preferred_format="bullets",
                tone=self.persona_tone,
            ),
        )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "bigquery": {},
        "qdrant": {},
        "model": {},
        "observability": {},
    }
    env_map = {
        ("GOOGLE_CLOUD_PROJECT",): ("bigquery", "project"),
        ("BIGQUERY_LOCATION",): ("bigquery", "location"),
        ("BQ_MAX_BYTES_BILLED",): ("bigquery", "max_bytes_billed"),
        ("QDRANT_URL",): ("qdrant", "url"),
        ("QDRANT_API_KEY",): ("qdrant", "api_key"),
        ("QDRANT_COLLECTION",): ("qdrant", "collection"),
        ("LLM_MODEL",): ("model", "llm_model"),
        ("EMBEDDING_PROVIDER",): ("model", "embedding_provider"),
        ("EMBEDDING_MODEL",): ("model", "embedding_model"),
        ("MAX_SQL_RETRIES",): ("model", "max_sql_retries"),
        ("MAX_CHAT_HISTORY_TURNS",): ("model", "max_history_turns"),
        ("MAX_CHAT_HISTORY_BYTES",): ("model", "max_history_bytes"),
        ("AGENT_LOG_PATH",): ("observability", "log_path"),
    }
    for env_names, path in env_map.items():
        for env_name in env_names:
            raw = os.getenv(env_name)
            if raw is None:
                continue
            section, field = path
            if field in {
                "max_bytes_billed",
                "max_sql_retries",
                "max_history_turns",
                "max_history_bytes",
            }:
                overrides[section][field] = int(raw)
            else:
                overrides[section][field] = raw
            break

    if os.getenv("LOGFIRE_TOKEN"):
        overrides["observability"]["enable_logfire"] = True

    return {k: v for k, v in overrides.items() if v}


@lru_cache
def load_config(config_path: str = "config/agent.yaml") -> AgentConfig:
    path = Path(config_path)
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}

    users = data.get("users", {})
    if isinstance(users, dict):
        data["users"] = {
            user_id: {"user_id": user_id, **profile}
            for user_id, profile in users.items()
        }

    data = _deep_merge(data, _env_overrides())
    return AgentConfig.model_validate(data)
