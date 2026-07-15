from __future__ import annotations

from typing import Any

import httpx
from google.genai.types import HttpRetryOptions
from pydantic_ai import ModelSettings
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider

from retail_agent.infrastructure.settings import ApplicationSettings

_RETRYABLE_HTTP_STATUS_CODES = [408, 429, 500, 502, 503, 504]
_PROVIDER_TRANSPORT_ERRORS = (
    ConnectionError,
    TimeoutError,
    httpx.ConnectError,
    httpx.TimeoutException,
)
_PROVIDER_FAILURE_ERRORS = (ModelAPIError, *_PROVIDER_TRANSPORT_ERRORS)


def analysis_model_settings(config: ApplicationSettings) -> ModelSettings:
    settings: dict[str, Any] = {
        "temperature": config.model.temperature,
        "max_tokens": config.model.max_output_tokens,
        "timeout": config.model.provider_request_timeout_seconds,
    }
    if config.model.llm_model.partition(":")[0] in {"google", "google-cloud"}:
        settings["google_thinking_config"] = {
            "thinking_budget": config.model.thinking_budget,
        }
    return ModelSettings(**settings)


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
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _RETRYABLE_HTTP_STATUS_CODES
    return isinstance(exc, _PROVIDER_TRANSPORT_ERRORS)


def provider_failure_details(
    exc: Exception,
    *,
    configured_attempts: int,
) -> dict[str, Any]:
    provider_exc = _nested_provider_exception(exc)
    status_code = provider_exc.status_code if isinstance(provider_exc, ModelHTTPError) else None
    if status_code == 429:
        category = "rate_limited"
    elif status_code in _RETRYABLE_HTTP_STATUS_CODES:
        category = "transient_provider_error"
    elif status_code is not None:
        category = "non_retryable_provider_error"
    elif isinstance(provider_exc, _PROVIDER_TRANSPORT_ERRORS):
        category = "provider_unavailable"
    elif isinstance(provider_exc, ModelAPIError):
        category = "provider_error"
    else:
        category = "non_provider_error"
    retry_count = (
        max(0, configured_attempts - 1)
        if status_code in _RETRYABLE_HTTP_STATUS_CODES
        or isinstance(provider_exc, _PROVIDER_TRANSPORT_ERRORS)
        else 0
    )
    if status_code is not None:
        provider_status = f"http_{status_code}"
    elif isinstance(provider_exc, _PROVIDER_TRANSPORT_ERRORS):
        provider_status = "transport_error"
    elif isinstance(provider_exc, ModelAPIError):
        provider_status = "provider_error"
    else:
        provider_status = "not_applicable"
    return {
        "provider_status": provider_status,
        "provider_status_code": status_code,
        "provider_retry_count": retry_count,
        "provider_terminal_category": category,
        "provider_error_category": category,
    }


def is_provider_failure(exc: Exception) -> bool:
    return isinstance(
        _nested_provider_exception(exc),
        _PROVIDER_FAILURE_ERRORS,
    )


def _nested_provider_exception(exc: Exception) -> Exception:
    if isinstance(exc, _PROVIDER_FAILURE_ERRORS):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in reversed(exc.exceptions):
            if isinstance(nested, Exception):
                provider_exc = _nested_provider_exception(nested)
                if isinstance(
                    provider_exc,
                    _PROVIDER_FAILURE_ERRORS,
                ):
                    return provider_exc
    return exc
