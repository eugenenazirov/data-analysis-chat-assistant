from __future__ import annotations

from retail_agent.bigquery import BigQueryRunner
from retail_agent.config import AgentConfig
from retail_agent.embeddings import GeminiEmbedder, HashingEmbedder
from retail_agent.golden_store import GoldenStore
from retail_agent.observability import EventLogger, maybe_configure_logfire


class Runtime:
    def __init__(self, config: AgentConfig):
        maybe_configure_logfire(config.observability.enable_logfire)
        self.config = config
        self.logger = EventLogger(config.observability.log_path)
        self.bigquery = BigQueryRunner(config, self.logger)
        self.embedder = (
            HashingEmbedder()
            if config.model.embedding_provider == "hash"
            else GeminiEmbedder(config)
        )
        self.golden_store = GoldenStore(config, self.embedder, self.logger)
