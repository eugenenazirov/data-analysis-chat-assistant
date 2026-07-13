from __future__ import annotations

from retail_agent.application.dto import (
    AgentAnalysisResult,
    AnalyzeQuestionResponse,
)
from retail_agent.application.ports import AnalysisAgent, ConversationRepository
from retail_agent.domain.models import (
    AgentFailure,
    AnalysisReport,
    Conversation,
    ConversationId,
    ConversationRole,
    ToolResultSummary,
    UserProfile,
    UserQuestion,
)
from retail_agent.domain.policies.privacy import redact_value


class AnalyzeQuestion:
    def __init__(
        self,
        agent: AnalysisAgent,
        conversations: ConversationRepository,
        *,
        max_retained_turns: int,
    ) -> None:
        self.agent = agent
        self.conversations = conversations
        self.max_retained_turns = max_retained_turns

    async def execute(
        self,
        question: str,
        *,
        user: UserProfile,
        conversation_id: str | None = None,
    ) -> AnalyzeQuestionResponse:
        user_question = UserQuestion(question)
        conversation = await self._load_or_start(conversation_id)
        result = await self.agent.analyze(user_question, conversation, user)
        result = _redact_analysis_result(result)

        conversation = conversation.append(
            ConversationRole.user,
            user_question.root,
        ).append(
            ConversationRole.assistant,
            _response_text(result),
            tool_result_summaries=result.tool_results,
        )
        await self.conversations.save(conversation)
        return AnalyzeQuestionResponse(
            response=result.response,
            conversation_id=str(conversation.id),
        )

    async def _load_or_start(self, conversation_id: str | None) -> Conversation:
        if conversation_id is None:
            return Conversation(max_retained_turns=self.max_retained_turns)
        identifier = ConversationId(conversation_id)
        existing = await self.conversations.get(identifier)
        return existing or Conversation(
            id=identifier,
            max_retained_turns=self.max_retained_turns,
        )


def _redact_analysis_result(result: AgentAnalysisResult) -> AgentAnalysisResult:
    redacted_response, _ = redact_value(result.response.model_dump())
    response = type(result.response).model_validate(redacted_response)
    redacted_tools, _ = redact_value(
        [summary.model_dump() for summary in result.tool_results]
    )
    return AgentAnalysisResult(
        response=response,
        tool_results=tuple(
            ToolResultSummary.model_validate(summary) for summary in redacted_tools
        ),
    )


def _response_text(result: AgentAnalysisResult) -> str:
    response = result.response
    if isinstance(response, AnalysisReport):
        return response.answer
    if isinstance(response, AgentFailure):
        return response.message
    raise TypeError(f"Unsupported analysis response: {type(response)!r}")
