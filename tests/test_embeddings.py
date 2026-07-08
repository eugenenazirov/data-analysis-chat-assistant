from __future__ import annotations

from types import SimpleNamespace

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
