from retail_agent.pii import redact_text, redact_value


def test_redact_text_masks_email_and_phone():
    text, count = redact_text("Email jane@example.com or call +1 415-555-0123.")

    assert "[REDACTED_EMAIL]" in text
    assert "[REDACTED_PHONE]" in text
    assert count == 2


def test_redact_value_recurses_through_dicts_and_lists():
    redacted, count = redact_value(
        {"rows": [{"email": "user@example.com", "notes": ["call 415-555-0123"]}]}
    )

    assert redacted["rows"][0]["email"] == "[REDACTED_EMAIL]"
    assert redacted["rows"][0]["notes"][0] == "call [REDACTED_PHONE]"
    assert count == 2
