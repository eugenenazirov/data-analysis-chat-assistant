from __future__ import annotations

from dataclasses import dataclass

from retail_agent.agent import build_analysis_agent
from retail_agent.application.dto import AnalyzeQuestionResponse
from retail_agent.application.use_cases import (
    AnalyzeQuestion,
    ClearConversation,
    StartConversation,
)
from retail_agent.domain.errors import QueryExecutionError
from retail_agent.domain.models import QueryResult
from retail_agent.infrastructure.agents.pydantic_ai_analysis_agent import (
    PydanticAIAnalysisAgent,
)
from retail_agent.infrastructure.analytics.bigquery_adapter import (
    BigQueryAnalyticsAdapter,
)
from retail_agent.infrastructure.conversations.in_memory_repository import (
    InMemoryConversationRepository,
)
from retail_agent.infrastructure.observability import (
    EventLogger,
    maybe_configure_logfire,
    new_trace_id,
)
from retail_agent.infrastructure.retrieval.gemini_embeddings import (
    GeminiEmbedder,
    HashingEmbedder,
)
from retail_agent.infrastructure.retrieval.qdrant_adapter import (
    QdrantGoldenExampleRepository,
)
from retail_agent.infrastructure.settings import ApplicationSettings, load_settings


class RuntimeOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BigQuerySmokeResult:
    query: QueryResult
    trace_id: str
    project: str | None
    dataset: str


class Runtime:
    def __init__(self, config: ApplicationSettings):
        maybe_configure_logfire(config.observability.enable_logfire)
        self.config = config
        self.logger = EventLogger(config.observability.log_path)
        self.bigquery = BigQueryAnalyticsAdapter(config, self.logger)
        self.embedder = (
            HashingEmbedder()
            if config.model.embedding_provider == "hash"
            else GeminiEmbedder(config)
        )
        self.golden_store = QdrantGoldenExampleRepository(
            config,
            self.embedder,
            self.logger,
        )
        self.analysis_agent = build_analysis_agent(config)
        self.conversations = InMemoryConversationRepository()
        self.agent_adapter = PydanticAIAnalysisAgent(
            config,
            self.bigquery,
            self.golden_store,
            self.logger,
            self.analysis_agent,
        )
        self.analyze_question = AnalyzeQuestion(
            self.agent_adapter,
            self.conversations,
            max_retained_turns=config.conversation.max_retained_turns,
        )
        self.start_conversation = StartConversation(
            self.conversations,
            max_retained_turns=config.conversation.max_retained_turns,
        )
        self.clear_conversation = ClearConversation(self.conversations)

    @classmethod
    def from_config_path(
        cls,
        config_path: str,
        *,
        require_retrieval: bool = False,
    ) -> Runtime:
        runtime = cls(load_settings(config_path))
        if require_retrieval:
            runtime.golden_store.wait_until_ready()
        return runtime

    async def analyze(
        self,
        question: str,
        *,
        user_id: str,
        conversation_id: str | None = None,
    ) -> AnalyzeQuestionResponse:
        return await self.analyze_question.execute(
            question,
            user=self.config.user_profile(user_id),
            conversation_id=conversation_id,
        )

    def index_golden(self, *, recreate: bool) -> int:
        trios = self.golden_store.load_seed_trios(self.config.golden_trios_path)
        return self.golden_store.index(trios, recreate=recreate)

    def bigquery_smoke(self, sql: str) -> BigQuerySmokeResult:
        trace_id = new_trace_id()
        self.logger.event(trace_id, "bigquery_smoke_started")
        try:
            result = self.bigquery.execute(sql, trace_id)
        except QueryExecutionError as exc:
            self.logger.event(trace_id, "bigquery_smoke_failed", error=str(exc))
            raise RuntimeOperationError(str(exc)) from exc
        self.logger.event(trace_id, "bigquery_smoke_completed", rows=result.rows)
        return BigQuerySmokeResult(
            query=result,
            trace_id=trace_id,
            project=self.config.bigquery.project,
            dataset=self.config.bigquery.dataset,
        )
