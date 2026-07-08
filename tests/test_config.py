from pathlib import Path

from retail_agent.config import load_config


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
    load_config.cache_clear()

    config = load_config(str(config_file))

    assert config.qdrant.url == "http://qdrant:6333"
    assert config.user_profile("manager_a").preferred_format == "table"
    assert config.user_profile("unknown").tone == "plain"
    assert isinstance(config.observability.log_path, Path)
