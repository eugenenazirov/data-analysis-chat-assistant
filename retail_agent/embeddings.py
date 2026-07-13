from __future__ import annotations

import hashlib
import math
from typing import Protocol

from retail_agent.config import AgentConfig


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]:
        ...


class GeminiEmbedder:
    def __init__(self, config: AgentConfig):
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError("google-genai is not installed.") from exc
            api_key = self.config.model.google_api_key
            if api_key is not None:
                self._client = genai.Client(api_key=api_key.get_secret_value())
            else:
                project = self.config.bigquery.project
                if not project:
                    raise RuntimeError(
                        "GOOGLE_CLOUD_PROJECT is required for Vertex AI embeddings "
                        "when GOOGLE_API_KEY is not set."
                    )
                self._client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=self.config.model.google_cloud_location,
                )
        return self._client

    def embed(self, text: str) -> list[float]:
        response = self.client.models.embed_content(
            model=self.config.model.embedding_model,
            contents=text,
        )
        embedding = response.embeddings[0]
        return list(embedding.values)


class HashingEmbedder:
    """Deterministic test embedder that avoids network calls."""

    def __init__(self, size: int = 64):
        self.size = size

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.size
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % self.size
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]
