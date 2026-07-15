from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from retail_agent.agent import build_analysis_agent
from retail_agent.application.dto import (
    AnalyzeQuestionResponse,
    ChartSmokeCase,
    ReviewerDiagnostics,
)
from retail_agent.application.use_cases import (
    AnalyzeQuestion,
    ClearConversation,
    StartConversation,
)
from retail_agent.domain.errors import QueryExecutionError
from retail_agent.domain.models import QueryResult
from retail_agent.infrastructure.agents.google_model import build_analysis_model
from retail_agent.infrastructure.agents.pydantic_ai_analysis_agent import (
    PydanticAIAnalysisAgent,
)
from retail_agent.infrastructure.analytics.bigquery_adapter import (
    BigQueryAnalyticsAdapter,
)
from retail_agent.infrastructure.charts import LocalPythonChartExecutor, run_chart_smoke
from retail_agent.infrastructure.conversations.in_memory_repository import (
    InMemoryConversationRepository,
)
from retail_agent.infrastructure.observability import (
    EventLogger,
    maybe_configure_logfire,
    new_trace_id,
)
from retail_agent.infrastructure.prompts.builder import PROMPT_VERSION
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
        self.chart_executor = LocalPythonChartExecutor(config.chart_execution)
        self.analysis_agent = build_analysis_agent(config)
        self.conversations = InMemoryConversationRepository()
        self.agent_adapter = PydanticAIAnalysisAgent(
            config,
            self.bigquery,
            self.golden_store,
            self.chart_executor,
            self.logger,
            self.analysis_agent,
            model_factory=lambda: build_analysis_model(config),
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

    @property
    def analysis_model(self) -> Any | None:
        return self.agent_adapter.analysis_model

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

    async def chart_smoke(self) -> tuple[ChartSmokeCase, ...]:
        artifacts = await run_chart_smoke(self.config.chart_execution)
        return tuple(ChartSmokeCase(case=item.case, artifact=item.artifact) for item in artifacts)

    def reviewer_diagnostics(self) -> ReviewerDiagnostics:
        image_revision = os.getenv("APP_REVISION", "development")
        worktree_revision = os.getenv("WORKTREE_REVISION")
        revision_matches = worktree_revision is None or image_revision == worktree_revision
        metadata_path = Path("/app/build-metadata.json")
        build_metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
        )
        stamped_prompt_version = str(build_metadata.get("prompt_version", "unknown"))
        prompt_matches = stamped_prompt_version == PROMPT_VERSION
        uses_vertex = self.config.model.llm_model.startswith("google-cloud:")
        values = (
            ("Application revision", image_revision),
            ("Working-tree revision", worktree_revision or "(not supplied)"),
            ("Revision match", "yes" if revision_matches else "NO"),
            ("Prompt version", PROMPT_VERSION),
            ("Stamped prompt version", stamped_prompt_version),
            ("Prompt match", "yes" if prompt_matches else "NO"),
            ("LLM model", self.config.model.llm_model),
            ("LLM route", "Vertex AI" if uses_vertex else "Gemini Developer API"),
            (
                "LLM location",
                self.config.model.google_cloud_llm_location if uses_vertex else "not applicable",
            ),
            (
                "LLM fallback location",
                (self.config.model.google_cloud_llm_fallback_location or "disabled")
                if uses_vertex
                else "not applicable",
            ),
            ("Embedding location", self.config.model.google_cloud_embedding_location),
            ("Provider attempts", str(self.config.model.provider_retry_attempts)),
            ("Thinking budget", str(self.config.model.thinking_budget)),
            ("Maximum model output", str(self.config.model.max_output_tokens)),
            ("BigQuery dataset", self.config.bigquery.dataset),
            ("Maximum returned rows", str(self.config.bigquery.max_result_rows)),
            ("Golden collection", self.config.retrieval.collection),
            ("Chart retry budget", str(self.config.agent_limits.max_chart_retries)),
            ("Matplotlib", version("matplotlib")),
            ("NumPy", version("numpy")),
            ("pandas", version("pandas")),
            ("seaborn", version("seaborn")),
        )
        return ReviewerDiagnostics(
            values=values,
            revision_matches=revision_matches,
            prompt_matches=prompt_matches,
        )
