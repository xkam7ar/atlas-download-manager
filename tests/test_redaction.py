from __future__ import annotations

import pytest

from atlas.redaction import redact_command_args, redact_text, redact_url, text_contains_secret


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example/file?Signature=secret&Expires=9",
        "https://cdn.example/file?sig=secret&se=9",
        "https://cdn.example/file?X-Amz-Signature=secret&X-Amz-Expires=9",
        "https://cdn.example/file?X-Goog-Signature=secret&X-Goog-Expires=9",
    ],
)
def test_signed_urls_are_sensitive(url: str) -> None:
    assert text_contains_secret(url)
    assert redact_command_args([url]) == ["<redacted>"]


def test_url_redaction_retains_safe_context() -> None:
    redacted = redact_url(
        "https://user:password@cdn.example/releases/file.iso?"
        "X-Amz-Signature=TOPSECRET&part=3"
    )

    assert redacted == (
        "https://cdn.example/releases/file.iso?X-Amz-Signature=<redacted>&part=3"
    )
    assert "user" not in redacted
    assert "password" not in redacted
    assert "TOPSECRET" not in redacted


def test_log_text_redacts_urls_and_key_value_secrets() -> None:
    message = redact_text(
        "request https://cdn.example/file?X-Goog-Signature=TOPSECRET, then token=SECOND"
    )

    assert message == "<redacted>"
