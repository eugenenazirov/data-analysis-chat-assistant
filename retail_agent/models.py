"""Compatibility exports for domain models.

New application code imports from ``retail_agent.domain.models`` directly.
"""

from retail_agent.domain.models import (
    AgentFailure,
    AnalysisReport,
    AnalysisResponse,
    ChartArtifact,
    ChartFormat,
    ChartRequest,
    ContextualizedQuestion,
    Conversation,
    ConversationId,
    ConversationRole,
    ConversationTurn,
    FailureCode,
    GoldenExample,
    GoldenTrio,
    QueryResult,
    RetrievedTrio,
    SafeSql,
    ToolResultSummary,
    UserPreferences,
    UserProfile,
    UserQuestion,
)

__all__ = [
    "AgentFailure",
    "AnalysisReport",
    "AnalysisResponse",
    "ChartArtifact",
    "ChartFormat",
    "ChartRequest",
    "ContextualizedQuestion",
    "Conversation",
    "ConversationId",
    "ConversationRole",
    "ConversationTurn",
    "FailureCode",
    "GoldenExample",
    "GoldenTrio",
    "QueryResult",
    "RetrievedTrio",
    "SafeSql",
    "ToolResultSummary",
    "UserPreferences",
    "UserProfile",
    "UserQuestion",
]
