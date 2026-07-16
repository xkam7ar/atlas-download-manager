from __future__ import annotations

import pytest

from atlas.redaction import (
    is_sensitive_header,
    redact_command_args,
    redact_structure,
    redact_text,
    redact_url,
    sanitize_terminal_text,
    text_contains_secret,
)


def test_terminal_text_sanitization_removes_escape_and_bidi_controls() -> None:
    value = "safe\x1b[2J\x1b]0;owned\x07name\u202eevil\x00.txt"

    assert sanitize_terminal_text(value) == "safenameevil.txt"


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
        "https://user:password@cdn.example/releases/file.iso?X-Amz-Signature=TOPSECRET&part=3"
    )

    assert redacted == ("https://cdn.example/releases/file.iso?X-Amz-Signature=<redacted>&part=3")
    assert "user" not in redacted
    assert "password" not in redacted
    assert "TOPSECRET" not in redacted


def test_oauth_fragment_redaction_retains_safe_context() -> None:
    value = "https://client.example/callback#access_token=TOPSECRET&state=expected"

    assert text_contains_secret(value)
    assert redact_url(value) == (
        "https://client.example/callback#access_token=<redacted>&state=expected"
    )


def test_route_fragment_query_redacts_authorization_code() -> None:
    value = "https://client.example/#/callback?code=TOPSECRET&state=expected"

    assert text_contains_secret(value)
    assert redact_url(value) == ("https://client.example/#/callback?code=<redacted>&state=expected")


def test_log_text_redacts_urls_and_key_value_secrets() -> None:
    message = redact_text(
        "request https://cdn.example/file?X-Goog-Signature=TOPSECRET, then token=SECOND"
    )

    assert message == "<redacted>"


@pytest.mark.parametrize(
    "header",
    [
        "Authentication: TOPSECRET",
        "X-Auth: TOPSECRET",
        "X-Api-Key: TOPSECRET",
        "X-Auth-Token: TOPSECRET",
        "X-Amz-Security-Token: TOPSECRET",
    ],
)
def test_command_redaction_hides_sensitive_custom_headers(header: str) -> None:
    assert redact_command_args(["--header", header]) == [
        "--header",
        f"{header.partition(':')[0]}: <redacted>",
    ]


def test_command_redaction_keeps_ordinary_headers_visible() -> None:
    assert redact_command_args(["--header=Accept-Language: en-US"]) == [
        "--header=Accept-Language: en-US"
    ]


@pytest.mark.parametrize("header", ["Authentication", "X-Auth", "X-Session"])
def test_sensitive_header_classifier_covers_credential_segments(header: str) -> None:
    assert is_sensitive_header(header)


def test_sensitive_header_classifier_does_not_confuse_author_metadata() -> None:
    assert not is_sensitive_header("X-Author")


@pytest.mark.parametrize(
    "args",
    [
        ["-u", "audit-user", "-p", "TOPSECRET"],
        ["--video-password", "TOPSECRET"],
        ["--ftp-password=TOPSECRET"],
        ["--all-proxy-passwd", "TOPSECRET"],
        ["--http-proxy-passwd", "TOPSECRET"],
        ["--client-certificate-key", "/private/key.pem"],
        ["-uUSERNAME", "-pTOPSECRET"],
    ],
)
def test_command_redaction_covers_backend_credential_aliases(args: list[str]) -> None:
    redacted = redact_command_args(args)

    assert "TOPSECRET" not in redacted
    assert "/private/key.pem" not in redacted


def test_text_redaction_covers_argv_style_passwords() -> None:
    assert redact_text("backend failed after --password TOPSECRET") == "<redacted>"


def test_structured_redaction_hides_aliases_without_erasing_session_metadata() -> None:
    payload = {
        "auth": "TOPSECRET",
        "jwt": "SECOND",
        "session": "THIRD",
        "ticket": "FOURTH",
        "session_type": "batch_session",
        "tls_session_file": "sessions.db",
    }

    assert redact_structure(payload) == {
        "auth": "<redacted>",
        "jwt": "<redacted>",
        "session": "<redacted>",
        "ticket": "<redacted>",
        "session_type": "batch_session",
        "tls_session_file": "sessions.db",
    }


@pytest.mark.parametrize(
    "name",
    ["apiKey", "AWSAccessKeyId", "sessionId", "X-Amz-Credential"],
)
def test_compact_query_secret_aliases_are_redacted(name: str) -> None:
    redacted = redact_url(f"https://example.com/file?{name}=TOPSECRET&part=2")

    assert "TOPSECRET" not in redacted
    assert "part=2" in redacted
