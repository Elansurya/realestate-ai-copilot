"""
Service layer for generating text embeddings used by the RAG pipeline.
Contains business logic only.
"""
from __future__ import annotations

import logging
from typing import Sequence

import httpx

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceException, ValidationException

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 64
MAX_CHARS_PER_INPUT = 8000


class EmbeddingService:
    """Generates vector embeddings for text chunks and queries."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.EMBEDDING_API_KEY
        self._base_url = settings.EMBEDDING_API_BASE_URL
        self._model = settings.EMBEDDING_MODEL
        self._dimensions = settings.EMBEDDING_DIMENSIONS
        self._timeout = settings.AI_REQUEST_TIMEOUT_SECONDS

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def generate_embedding(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValidationException("Cannot generate an embedding for empty text.")
        results = await self.generate_embeddings_batch([text])
        return results[0]

    async def generate_embeddings_batch(
        self, texts: Sequence[str]
    ) -> list[list[float]]:
        if not texts:
            raise ValidationException("At least one text input is required.")

        cleaned = [t.strip()[:MAX_CHARS_PER_INPUT] for t in texts if t and t.strip()]
        if not cleaned:
            raise ValidationException("All provided text inputs are empty.")

        results: list[list[float]] = []
        for start in range(0, len(cleaned), MAX_BATCH_SIZE):
            batch = cleaned[start : start + MAX_BATCH_SIZE]
            results.extend(await self._call_embedding_api(batch))
        return results

    async def _call_embedding_api(self, batch: Sequence[str]) -> list[list[float]]:
        api_key = (
            self._api_key.get_secret_value()
            if hasattr(self._api_key, "get_secret_value")
            else self._api_key
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self._model, "input": list(batch)}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            logger.error("Embedding provider request failed: %s", exc)
            raise ExternalServiceException(
                "The embedding provider is currently unavailable. Please try again shortly."
            ) from exc

        items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
        embeddings = [item["embedding"] for item in items]

        if len(embeddings) != len(batch):
            raise ExternalServiceException(
                "Embedding provider returned an unexpected number of results."
            )
        return embeddings