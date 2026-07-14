import sys
from types import SimpleNamespace

from retail_agent.infrastructure.observability import maybe_configure_logfire


def test_logfire_instrumentation_excludes_model_content(monkeypatch):
    calls = []
    fake_logfire = SimpleNamespace(
        configure=lambda: calls.append(("configure", {})),
        instrument_pydantic_ai=lambda **kwargs: calls.append(("instrument", kwargs)),
    )
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    maybe_configure_logfire(True)

    assert calls == [
        ("configure", {}),
        (
            "instrument",
            {"include_content": False, "include_binary_content": False},
        ),
    ]
