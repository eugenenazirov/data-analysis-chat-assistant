from __future__ import annotations

from typing import Any

from google.genai.types import HttpRetryOptions
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider

from retail_agent.infrastructure.settings import ApplicationSettings

_RETRYABLE_HTTP_STATUS_CODES = [408, 429, 500, 502, 503, 504]


def build_analysis_model(config: ApplicationSettings) -> Any:
    """Build a reusable model with bounded cross-endpoint transport attempts."""

    provider_name, separator, model_name = config.model.llm_model.partition(":")
    if not separator or provider_name not in {"google", "google-cloud"}:
        return config.model.llm_model

    def retry_options(attempts: int) -> HttpRetryOptions:
        return HttpRetryOptions(
            attempts=attempts,
            initial_delay=config.model.provider_retry_initial_delay,
            max_delay=config.model.provider_retry_max_delay,
            exp_base=2,
            jitter=1,
            http_status_codes=_RETRYABLE_HTTP_STATUS_CODES,
        )

    configured_attempts = config.model.provider_retry_attempts
    fallback_location = config.model.google_cloud_llm_fallback_location
    use_regional_fallback = (
        provider_name == "google-cloud"
        and fallback_location is not None
        and fallback_location != config.model.google_cloud_llm_location
        and configured_attempts > 1
    )
    primary_attempts = configured_attempts - 1 if use_regional_fallback else configured_attempts
    api_key = (
        config.model.google_api_key.get_secret_value()
        if config.model.google_api_key is not None
        else None
    )
    if provider_name == "google-cloud":
        provider = GoogleCloudProvider(
            project=config.bigquery.project,
            location=config.model.google_cloud_llm_location,
            retry_options=retry_options(primary_attempts),
        )
    else:
        provider = GoogleProvider(
            api_key=api_key,
            retry_options=retry_options(primary_attempts),
        )
    primary = GoogleModel(model_name, provider=provider)
    if not use_regional_fallback:
        return primary

    fallback_provider = GoogleCloudProvider(
        project=config.bigquery.project,
        location=fallback_location,
        retry_options=retry_options(1),
    )
    fallback = GoogleModel(model_name, provider=fallback_provider)
    return FallbackModel(
        primary,
        fallback,
        fallback_on=_is_retryable_provider_failure,
    )


def _is_retryable_provider_failure(exc: Exception) -> bool:
    return isinstance(exc, ModelHTTPError) and exc.status_code in _RETRYABLE_HTTP_STATUS_CODES
