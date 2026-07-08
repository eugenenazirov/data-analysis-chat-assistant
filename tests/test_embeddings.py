from __future__ import annotations

from types import SimpleNamespace

import pytest
from google import genai

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

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setattr(genai, "Client", fake_client)

    embedder = GeminiEmbedder(test_config)

    assert embedder.client.models
    assert calls == [
        {
            "vertexai": True,
            "project": "test-project",
            "location": "europe-west4",
        }
    ]


def test_gemini_embedder_requires_project_for_vertex_ai(test_config, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    embedder = GeminiEmbedder(test_config)

    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = embedder.client
