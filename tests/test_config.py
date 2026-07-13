from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from retail_agent.config import (
    AgentConfig,
    AgentLimitSettings,
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
    monkeypatch.setenv("MAX_CHAT_HISTORY_TURNS", "4")
    monkeypatch.setenv("MAX_CHAT_HISTORY_BYTES", "8192")
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.retrieval.url == "http://qdrant:6333"
    assert config.retrieval.top_k == 5
    assert config.agent_limits.max_sql_retries == 0
    assert config.conversation.max_history_turns == 4
    assert config.conversation.max_history_bytes == 8192
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
