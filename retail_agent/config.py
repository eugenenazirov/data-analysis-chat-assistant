from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from retail_agent.models import UserProfile


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
    max_sql_retries: int = 2
    temperature: float = 0.1


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
            "latitude",
            "longitude",
            "user_geom",
        ]
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
        ("AGENT_LOG_PATH",): ("observability", "log_path"),
    }
    for env_names, path in env_map.items():
        for env_name in env_names:
            raw = os.getenv(env_name)
            if raw is None:
                continue
            section, field = path
            if field in {"max_bytes_billed"}:
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
