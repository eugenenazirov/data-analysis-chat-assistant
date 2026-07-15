from __future__ import annotations

from types import SimpleNamespace

import pytest
from google import genai
from pydantic import SecretStr

from retail_agent.domain.errors import RetrievalError
from retail_agent.embeddings import GeminiEmbedder


class FakeModels:
    def __init__(self):
        self.calls = []

    def embed_content(self, *, model: str, contents: str):
        self.calls.append({"model": model, "contents": contents})
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.25, -0.5, 0.75])]
        )


class FakeGeminiClient:
    def __init__(self):
        self.models = FakeModels()


def test_gemini_embedder_uses_configured_model_with_mock_client(test_config):
    client = FakeGeminiClient()
    embedder = GeminiEmbedder(test_config)
    embedder._client = client

    vector = embedder.embed("monthly revenue by category")

    assert vector == [0.25, -0.5, 0.75]
    assert client.models.calls == [
        {
            "model": test_config.model.embedding_model,
            "contents": "monthly revenue by category",
        }
    ]


def test_gemini_embedder_uses_vertex_ai_when_api_key_is_absent(
    test_config, monkeypatch
):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return FakeGeminiClient()

    monkeypatch.setattr(genai, "Client", fake_client)
    configured = test_config.model_copy(
        update={
            "bigquery": test_config.bigquery.model_copy(
                update={"project": "test-project"}
            ),
            "model": test_config.model.model_copy(
                update={
                    "google_api_key": None,
                    "google_cloud_embedding_location": "europe-west4",
                }
            ),
        }
    )

    embedder = GeminiEmbedder(configured)

    assert embedder.client.models
    assert calls == [
        {
            "vertexai": True,
            "project": "test-project",
            "location": "europe-west4",
        }
    ]


def test_gemini_embedder_requires_project_for_vertex_ai(test_config):
    configured = test_config.model_copy(
        update={
            "bigquery": test_config.bigquery.model_copy(update={"project": None}),
            "model": test_config.model.model_copy(update={"google_api_key": None}),
        }
    )
    embedder = GeminiEmbedder(configured)

    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = embedder.client


def test_gemini_embedder_uses_configured_api_key(test_config, monkeypatch):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return FakeGeminiClient()

    monkeypatch.setattr(genai, "Client", fake_client)
    configured = test_config.model_copy(
        update={
            "model": test_config.model.model_copy(
                update={
                    "llm_model": "google:gemini-3.5-flash",
                    "google_api_key": SecretStr("api-secret"),
                }
            )
        }
    )
    embedder = GeminiEmbedder(configured)

    assert embedder.client.models
    assert calls == [{"api_key": "api-secret"}]


def test_gemini_embedder_uses_vertex_with_cloud_model_even_when_api_key_exists(
    test_config, monkeypatch
):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return FakeGeminiClient()

    monkeypatch.setattr(genai, "Client", fake_client)
    configured = test_config.model_copy(
        update={
            "bigquery": test_config.bigquery.model_copy(update={"project": "test-project"}),
            "model": test_config.model.model_copy(
                update={
                    "llm_model": "google-cloud:gemini-3.5-flash",
                    "google_api_key": SecretStr("developer-key-must-not-be-used"),
                    "google_cloud_embedding_location": "us-central1",
                }
            ),
        }
    )

    embedder = GeminiEmbedder(configured)

    assert embedder.client.models
    assert calls == [
        {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
        }
    ]


def test_gemini_embedder_translates_provider_failure(test_config):
    class FailingModels:
        def embed_content(self, **kwargs):
            raise ConnectionError("provider detail")

    embedder = GeminiEmbedder(test_config)
    embedder._client = SimpleNamespace(models=FailingModels())

    with pytest.raises(RetrievalError, match="Embedding request failed"):
        embedder.embed("question")
