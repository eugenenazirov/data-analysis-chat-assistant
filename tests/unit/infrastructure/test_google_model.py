from __future__ import annotations

from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from retail_agent.infrastructure.agents import google_model


def test_analysis_model_settings_bound_each_provider_attempt(test_config):
    configured = test_config.model_copy(
        update={
            "model": test_config.model.model_copy(
                update={"provider_request_timeout_seconds": 90}
            )
        }
    )

    settings = google_model.analysis_model_settings(configured)

    assert settings["timeout"] == 90


def test_google_cloud_model_uses_global_provider_and_bounded_transport_retries(
    test_config, monkeypatch
):
    providers = []

    def fake_provider(**kwargs):
        provider = SimpleNamespace(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(google_model, "GoogleCloudProvider", fake_provider)
    monkeypatch.setattr(
        google_model,
        "GoogleModel",
        lambda name, *, provider: SimpleNamespace(name=name, provider=provider),
    )
    monkeypatch.setattr(
        google_model,
        "FallbackModel",
        lambda primary, fallback, **kwargs: SimpleNamespace(models=[primary, fallback], **kwargs),
    )
    configured = test_config.model_copy(
        update={
            "bigquery": test_config.bigquery.model_copy(update={"project": "review-project"}),
            "model": test_config.model.model_copy(
                update={
                    "llm_model": "google-cloud:gemini-2.5-flash",
                    "google_cloud_llm_location": "global",
                    "google_cloud_llm_fallback_location": "us-central1",
                    "google_api_key": SecretStr("ai-studio-key-must-not-be-used"),
                    "provider_retry_attempts": 3,
                    "provider_retry_initial_delay": 1,
                    "provider_retry_max_delay": 4,
                }
            ),
        }
    )

    model = google_model.build_analysis_model(configured)

    assert [item.name for item in model.models] == [
        "gemini-2.5-flash",
        "gemini-2.5-flash",
    ]
    assert providers[0].project == "review-project"
    assert providers[0].location == "global"
    assert not hasattr(providers[0], "api_key")
    assert providers[1].project == "review-project"
    assert providers[1].location == "us-central1"
    retry = providers[0].retry_options
    assert retry.attempts == 2
    assert retry.initial_delay == 1
    assert retry.max_delay == 4
    assert retry.exp_base == 2
    assert retry.jitter == 1
    assert retry.http_status_codes == [408, 429, 500, 502, 503, 504]
    assert providers[1].retry_options.attempts == 1


def test_google_cloud_model_skips_duplicate_fallback_location(test_config, monkeypatch):
    monkeypatch.setattr(
        google_model,
        "GoogleCloudProvider",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        google_model,
        "GoogleModel",
        lambda name, *, provider: SimpleNamespace(name=name, provider=provider),
    )
    configured = test_config.model_copy(
        update={
            "model": test_config.model.model_copy(
                update={
                    "llm_model": "google-cloud:gemini-3.5-flash",
                    "google_cloud_llm_location": "us-central1",
                    "google_cloud_llm_fallback_location": "us-central1",
                }
            )
        }
    )

    model = google_model.build_analysis_model(configured)

    assert model.provider.location == "us-central1"
    assert model.provider.retry_options.attempts == 3


def test_google_developer_model_uses_api_key_and_all_transport_attempts(
    test_config, monkeypatch
):
    providers = []

    def fake_provider(**kwargs):
        provider = SimpleNamespace(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(google_model, "GoogleProvider", fake_provider)
    monkeypatch.setattr(
        google_model,
        "GoogleModel",
        lambda name, *, provider: SimpleNamespace(name=name, provider=provider),
    )
    configured = test_config.model_copy(
        update={
            "model": test_config.model.model_copy(
                update={
                    "llm_model": "google:gemini-3.5-flash",
                    "google_api_key": SecretStr("developer-api-key"),
                    "google_cloud_llm_location": "global",
                    "google_cloud_llm_fallback_location": "us-central1",
                    "provider_retry_attempts": 3,
                }
            )
        }
    )

    model = google_model.build_analysis_model(configured)

    assert model.name == "gemini-3.5-flash"
    assert len(providers) == 1
    assert providers[0].api_key == "developer-api-key"
    assert providers[0].retry_options.attempts == 3
    assert not hasattr(providers[0], "location")


def test_non_google_model_is_left_for_pydantic_ai_to_resolve(test_config):
    configured = test_config.model_copy(
        update={"model": test_config.model.model_copy(update={"llm_model": "test"})}
    )

    assert google_model.build_analysis_model(configured) == "test"


def test_transport_timeout_is_retryable_and_has_safe_telemetry():
    failure = httpx.ReadTimeout("provider request timed out")

    assert google_model._is_retryable_provider_failure(failure) is True
    assert google_model.is_provider_failure(failure) is True
    assert google_model.provider_failure_details(
        failure,
        configured_attempts=3,
    ) == {
        "provider_status": "transport_error",
        "provider_status_code": None,
        "provider_retry_count": 2,
        "provider_terminal_category": "provider_unavailable",
        "provider_error_category": "provider_unavailable",
    }
