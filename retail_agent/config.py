"""Compatibility exports for structured application settings."""

from retail_agent.infrastructure.settings import (
    DEFAULT_HISTORY_BYTES,
    DEFAULT_SQL_RETRIES,
    AgentLimitSettings,
    ApplicationSettings,
    BigQuerySettings,
    ChartExecutionSettings,
    ConversationSettings,
    ModelSettings,
    ObservabilitySettings,
    RetrievalSettings,
    SafetySettings,
    load_settings,
)

AgentConfig = ApplicationSettings
BigQueryConfig = BigQuerySettings
ModelConfig = ModelSettings
ObservabilityConfig = ObservabilitySettings
QdrantConfig = RetrievalSettings
SafetyConfig = SafetySettings
load_config = load_settings

__all__ = [
    "DEFAULT_HISTORY_BYTES",
    "DEFAULT_SQL_RETRIES",
    "AgentConfig",
    "AgentLimitSettings",
    "ApplicationSettings",
    "BigQueryConfig",
    "BigQuerySettings",
    "ChartExecutionSettings",
    "ConversationSettings",
    "ModelConfig",
    "ModelSettings",
    "ObservabilityConfig",
    "ObservabilitySettings",
    "QdrantConfig",
    "RetrievalSettings",
    "SafetyConfig",
    "SafetySettings",
    "load_config",
    "load_settings",
]
