from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from retail_agent.config import (
    AgentConfig,
    AgentLimitSettings,
    ModelConfig,
    QdrantConfig,
    load_config,
)


def test_load_config_user_profiles(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text(
        """
persona_tone: "plain"
users:
  manager_a:
    display_name: "Manager A"
    preferred_format: "table"
    tone: "numbers first"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("GOLDEN_TOP_K", "5")
    monkeypatch.setenv("MAX_SQL_RETRIES", "0")
    monkeypatch.setenv("MAX_CHART_RETRIES", "1")
    monkeypatch.setenv("MAX_CHAT_HISTORY_TURNS", "4")
    monkeypatch.setenv("MAX_CHAT_HISTORY_BYTES", "8192")
    monkeypatch.setenv("LLM_THINKING_BUDGET", "0")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "90")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.retrieval.url == "http://qdrant:6333"
    assert config.retrieval.top_k == 5
    assert config.agent_limits.max_sql_retries == 0
    assert config.agent_limits.max_chart_retries == 1
    assert config.conversation.max_history_turns == 4
    assert config.conversation.max_history_bytes == 8192
    assert config.model.thinking_budget == 0
    assert config.model.provider_request_timeout_seconds == 90
    assert config.user_profile("manager_a").preferred_format == "table"
    assert config.user_profile("unknown").tone == "plain"
    assert isinstance(config.observability.log_path, Path)


@pytest.mark.parametrize("retry_budget", [-1, 4])
def test_model_config_rejects_unbounded_retry_budget(retry_budget):
    with pytest.raises(ValidationError):
        AgentLimitSettings(max_sql_retries=retry_budget)


def test_agent_config_defaults_to_bounded_history():
    assert AgentConfig().conversation.max_history_turns == 6
    assert AgentConfig().conversation.max_history_bytes == 65536


def test_gemini_defaults_to_deterministic_low_latency_settings():
    model = ModelConfig()

    assert model.temperature == 0
    assert model.thinking_budget == 0
    assert model.max_output_tokens == 2048
    assert model.provider_request_timeout_seconds == 120
    assert model.google_cloud_llm_location == "global"
    assert model.google_cloud_llm_fallback_location == "us-central1"
    assert AgentLimitSettings().output_retries == 1


@pytest.mark.parametrize("top_k", [0, 21])
def test_qdrant_config_rejects_unbounded_retrieval_limit(top_k):
    with pytest.raises(ValidationError):
        QdrantConfig(top_k=top_k)


def test_settings_mask_secrets_when_serialized():
    config = AgentConfig(
        retrieval=QdrantConfig(api_key=SecretStr("qdrant-secret")),
    )

    serialized = config.model_dump_json()

    assert "qdrant-secret" not in serialized
    assert "**********" in serialized


def test_settings_source_precedence(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text("retrieval:\n  top_k: 2\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GOLDEN_TOP_K=4\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOLDEN_TOP_K", "5")
    load_config.cache_clear()

    assert load_config(str(config_file)).retrieval.top_k == 5

    explicit = AgentConfig(retrieval=QdrantConfig(top_k=6))
    assert explicit.retrieval.top_k == 6

    monkeypatch.delenv("GOLDEN_TOP_K")
    load_config.cache_clear()
    assert load_config(str(config_file)).retrieval.top_k == 4

    (tmp_path / ".env").unlink()
    load_config.cache_clear()
    assert load_config(str(config_file)).retrieval.top_k == 2


def test_legacy_google_location_configures_both_clients(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.model.google_cloud_llm_location == "europe-west4"
    assert config.model.google_cloud_embedding_location == "europe-west4"


def test_specific_google_locations_override_legacy_value(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("GOOGLE_CLOUD_LLM_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_CLOUD_EMBEDDING_LOCATION", "us-central1")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.model.google_cloud_llm_location == "global"
    assert config.model.google_cloud_embedding_location == "us-central1"


def test_google_llm_fallback_location_can_be_overridden(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLOUD_LLM_FALLBACK_LOCATION", "us-east4")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.model.google_cloud_llm_fallback_location == "us-east4"


def test_unrelated_model_environment_variable_does_not_break_settings(
    tmp_path, monkeypatch
):
    config_file = tmp_path / "agent.yaml"
    config_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("MODEL", "google-cloud:gemini-2.5-flash")
    monkeypatch.setenv("LLM_MODEL", "google-cloud:gemini-2.5-flash")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.model.llm_model == "google-cloud:gemini-2.5-flash"
