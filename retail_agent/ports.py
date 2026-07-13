from __future__ import annotations

from retail_agent.application.ports import AnalyticsGateway, GoldenExampleRepository
from retail_agent.infrastructure.agents.runner import AnalysisAgentRunner

WarehousePort = AnalyticsGateway
KnowledgeRetrieverPort = GoldenExampleRepository
AnalysisAgentPort = AnalysisAgentRunner
