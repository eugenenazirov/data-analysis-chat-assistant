from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from retail_agent.domain.models import UserProfile

DEFAULT_SQL_RETRIES = 2
DEFAULT_HISTORY_BYTES = 64 * 1024


class BigQuerySettings(BaseModel):
    project: str | None = None
    location: str = "US"
    dataset: str = "bigquery-public-data.thelook_ecommerce"
    allowed_tables: list[str] = Field(
        default_factory=lambda: ["orders", "order_items", "products", "users"],
        min_length=1,
    )
    max_bytes_billed: int = Field(default=200_000_000, gt=0)
    timeout_seconds: int = Field(default=60, gt=0, le=600)
    max_result_rows: int = Field(default=500, gt=0, le=10_000)
    job_label_app: str = "retail-agent"


class RetrievalSettings(BaseModel):
    url: str = "http://localhost:6333"
    api_key: SecretStr | None = None
    collection: str = "golden_trios"
    timeout_seconds: int = Field(default=30, gt=0, le=300)
    top_k: int = Field(default=3, ge=1, le=20)


class ModelSettings(BaseModel):
    llm_model: str = "google-cloud:gemini-3.5-flash"
    embedding_provider: Literal["gemini", "hash"] = "gemini"
    embedding_model: str = "gemini-embedding-001"
    google_api_key: SecretStr | None = None
    google_cloud_location: str | None = None
    google_cloud_llm_location: str = "global"
    google_cloud_llm_fallback_location: str | None = "us-central1"
    google_cloud_embedding_location: str = "us-central1"
    provider_retry_attempts: int = Field(default=3, ge=1, le=5)
    provider_retry_initial_delay: float = Field(default=1.0, ge=0, le=30)
    provider_retry_max_delay: float = Field(default=4.0, ge=0, le=60)
    thinking_budget: int = Field(default=0, ge=-1, le=24_576)
    max_output_tokens: int = Field(default=2_048, ge=256, le=65_536)
    temperature: float = Field(default=0.0, ge=0, le=2)


class AgentLimitSettings(BaseModel):
    request_limit: int = Field(default=8, ge=1, le=50)
    tool_calls_limit: int = Field(default=6, ge=1, le=50)
    total_tokens_limit: int = Field(default=32_000, ge=1_000)
    max_sql_retries: int = Field(default=DEFAULT_SQL_RETRIES, ge=0, le=3)
    max_chart_retries: int = Field(default=2, ge=0, le=3)
    output_retries: int = Field(default=1, ge=0, le=3)


class ChartExecutionSettings(BaseModel):
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_source_bytes: int = Field(default=20_000, ge=1_000, le=100_000)
    max_output_bytes: int = Field(default=5_000_000, ge=1_000, le=20_000_000)
    max_captured_output_bytes: int = Field(default=16_384, ge=1_024, le=1_000_000)
    artifact_directory: Path = Path("artifacts/charts")


class ConversationSettings(BaseModel):
    max_history_turns: int = Field(default=6, ge=1, le=20)
    max_history_bytes: int = Field(
        default=DEFAULT_HISTORY_BYTES,
        ge=4_096,
        le=1_048_576,
    )
    max_retained_turns: int = Field(default=20, ge=2, le=200)


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


class SafetySettings(BaseModel):
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


class ObservabilitySettings(BaseModel):
    log_path: Path = Path("logs/agent-runs.jsonl")
    enable_logfire: bool = False
    logfire_token: SecretStr | None = None


_ENV_ALIASES: dict[str, tuple[str, str]] = {
    "GOOGLE_CLOUD_PROJECT": ("bigquery", "project"),
    "BIGQUERY_LOCATION": ("bigquery", "location"),
    "BQ_MAX_BYTES_BILLED": ("bigquery", "max_bytes_billed"),
    "QDRANT_URL": ("retrieval", "url"),
    "QDRANT_API_KEY": ("retrieval", "api_key"),
    "QDRANT_COLLECTION": ("retrieval", "collection"),
    "GOLDEN_TOP_K": ("retrieval", "top_k"),
    "LLM_MODEL": ("model", "llm_model"),
    "EMBEDDING_PROVIDER": ("model", "embedding_provider"),
    "EMBEDDING_MODEL": ("model", "embedding_model"),
    "GOOGLE_API_KEY": ("model", "google_api_key"),
    "GOOGLE_CLOUD_LOCATION": ("model", "google_cloud_location"),
    "GOOGLE_CLOUD_LLM_LOCATION": ("model", "google_cloud_llm_location"),
    "GOOGLE_CLOUD_LLM_FALLBACK_LOCATION": (
        "model",
        "google_cloud_llm_fallback_location",
    ),
    "GOOGLE_CLOUD_EMBEDDING_LOCATION": (
        "model",
        "google_cloud_embedding_location",
    ),
    "LLM_THINKING_BUDGET": ("model", "thinking_budget"),
    "LLM_MAX_OUTPUT_TOKENS": ("model", "max_output_tokens"),
    "MAX_SQL_RETRIES": ("agent_limits", "max_sql_retries"),
    "MAX_CHART_RETRIES": ("agent_limits", "max_chart_retries"),
    "MAX_AGENT_REQUESTS": ("agent_limits", "request_limit"),
    "MAX_TOOL_CALLS": ("agent_limits", "tool_calls_limit"),
    "MAX_AGENT_TOKENS": ("agent_limits", "total_tokens_limit"),
    "MAX_OUTPUT_RETRIES": ("agent_limits", "output_retries"),
    "MAX_CHAT_HISTORY_TURNS": ("conversation", "max_history_turns"),
    "MAX_CHAT_HISTORY_BYTES": ("conversation", "max_history_bytes"),
    "CHART_TIMEOUT_SECONDS": ("chart_execution", "timeout_seconds"),
    "AGENT_LOG_PATH": ("observability", "log_path"),
    "LOGFIRE_TOKEN": ("observability", "logfire_token"),
}


class _FlatAliasSettingsSource(PydanticBaseSettingsSource):
    def __init__(
        self,
        settings_cls: type[BaseSettings],
        values: Mapping[str, str | None],
    ) -> None:
        super().__init__(settings_cls)
        self._values = {key.casefold(): value for key, value in values.items()}

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for alias, (section, field) in _ENV_ALIASES.items():
            value = self._values.get(alias.casefold())
            if value in {None, ""}:
                continue
            result.setdefault(section, {})[field] = value
        legacy_location = self._values.get("google_cloud_location")
        if legacy_location not in {None, ""}:
            model = result.setdefault("model", {})
            model.setdefault("google_cloud_llm_location", legacy_location)
            model.setdefault("google_cloud_embedding_location", legacy_location)
        if result.get("observability", {}).get("logfire_token"):
            result["observability"]["enable_logfire"] = True
        return result


class ApplicationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        env_nested_delimiter="__",
        env_prefix="RETAIL_AGENT_",
        extra="ignore",
        yaml_file="config/agent.yaml",
        yaml_file_encoding="utf-8",
    )

    yaml_path: ClassVar[Path] = Path("config/agent.yaml")

    model: ModelSettings = Field(default_factory=ModelSettings)
    bigquery: BigQuerySettings = Field(default_factory=BigQuerySettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    agent_limits: AgentLimitSettings = Field(default_factory=AgentLimitSettings)
    chart_execution: ChartExecutionSettings = Field(default_factory=ChartExecutionSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    persona_tone: str = "clear, concise, executive-friendly"
    persona_version: str = "prototype-config-v1"
    users: dict[str, UserProfile] = Field(default_factory=dict)
    golden_trios_path: Path = Path("data/golden_trios.jsonl")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        direct_env = EnvSettingsSource(settings_cls)
        dotenv = DotEnvSettingsSource(settings_cls)
        return (
            init_settings,
            _FlatAliasSettingsSource(settings_cls, direct_env.env_vars),
            env_settings,
            _FlatAliasSettingsSource(settings_cls, dotenv.env_vars),
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, deep_merge=True),
            file_secret_settings,
        )

    @field_validator("users", mode="before")
    @classmethod
    def populate_user_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            user_id: {"user_id": user_id, **profile} if isinstance(profile, dict) else profile
            for user_id, profile in value.items()
        }

    @model_validator(mode="after")
    def validate_safe_table_configuration(self) -> ApplicationSettings:
        missing = set(self.bigquery.allowed_tables) - set(self.safety.safe_columns_by_table)
        if missing:
            tables = ", ".join(sorted(missing))
            raise ValueError(f"Missing safety.safe_columns_by_table entries for: {tables}.")
        return self

    @property
    def qdrant(self) -> RetrievalSettings:
        """Compatibility accessor for existing infrastructure adapters."""

        return self.retrieval

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


def _settings_class(config_path: str) -> type[ApplicationSettings]:
    configured = dict(ApplicationSettings.model_config)
    configured["yaml_file"] = config_path
    return type(
        "ConfiguredApplicationSettings",
        (ApplicationSettings,),
        {"model_config": SettingsConfigDict(**configured)},
    )


@lru_cache
def load_settings(config_path: str = "config/agent.yaml") -> ApplicationSettings:
    return _settings_class(config_path)()
