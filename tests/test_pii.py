from retail_agent.pii import redact_text, redact_value


def test_redact_text_masks_email_and_phone():
    text, count = redact_text("Email jane@example.com or call +1 415-555-0123.")

    assert "[REDACTED_EMAIL]" in text
    assert "[REDACTED_PHONE]" in text
    assert count == 2


def test_redact_value_recurses_through_nested_collections():
    redacted, count = redact_value(
        {"rows": ({"email": "user@example.com", "notes": ["call 415-555-0123"]},)}
    )

    assert redacted["rows"][0]["email"] == "[REDACTED_EMAIL]"
    assert redacted["rows"][0]["notes"][0] == "call [REDACTED_PHONE]"
    assert count == 2


def test_redact_text_does_not_treat_business_numbers_as_phone_numbers():
    text, count = redact_text(
        "Revenue was 1330431.52 across 8200 orders; customer ID 1000001 was aggregated."
    )

    assert text == (
        "Revenue was 1330431.52 across 8200 orders; customer ID 1000001 was aggregated."
    )
    assert count == 0
