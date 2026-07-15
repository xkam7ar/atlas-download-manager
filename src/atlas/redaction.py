"""Small, shared helpers for keeping credentials out of human and JSON output."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "keyring",
        "password",
        "passwd",
        "proxy",
        "secret",
        "token",
    }
)
_SENSITIVE_HEADER_NAMES = frozenset({"authorization", "cookie", "proxy-authorization"})
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "key_pair_id",
        "policy",
        "po_token",
        "shared_access_signature",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
_SENSITIVE_FLAGS = frozenset(
    {
        "--body-data",
        "--body-file",
        "--cookie",
        "--cookies",
        "--http-password",
        "--http-passwd",
        "--http-user",
        "--load-cookies",
        "--netrc-file",
        "--password",
        "--post-data",
        "--post-file",
        "--proxy",
        "--proxy-password",
        "--proxy-user",
        "--rpc-secret",
        "--save-cookies",
        "--user",
    }
)
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(?:^|[\s;,:])(?:access[_-]?token|api[_-]?key|authorization|cookie|po_token|"
    r"password|proxy[_-]?password|secret|token)\s*[:=]"
)
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


def is_sensitive_key(key: str) -> bool:
    """Return whether a structured field name can contain credential material."""

    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def text_contains_secret(value: str) -> bool:
    """Detect credential-bearing URLs and common key/value secret forms."""

    if _SENSITIVE_TEXT_PATTERN.search(value):
        return True
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    return any(_is_sensitive_query_key(name) for name, _item in parse_qsl(parsed.query))


def redact_url(value: str) -> str:
    """Redact credentials and signed query values while retaining URL context."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    query = urlencode(
        [
            (name, "<redacted>" if _is_sensitive_query_key(name) else item)
            for name, item in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
        safe="<>",
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def redact_text(value: str) -> str:
    """Redact URL and key/value secrets from an arbitrary log message."""

    def replace_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        base = candidate.rstrip(_TRAILING_URL_PUNCTUATION)
        trailing = candidate[len(base) :]
        return f"{redact_url(base)}{trailing}"

    redacted = _URL_PATTERN.sub(replace_url, value)
    if _SENSITIVE_TEXT_PATTERN.search(redacted):
        return "<redacted>"
    return redacted


def redact_command_args(args: Sequence[object]) -> list[object]:
    """Return a safe command preview while retaining its useful flag structure."""

    redacted: list[object] = []
    redact_next = False
    header_next = False
    for arg in args:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if header_next:
            redacted.append(_redact_header_value(arg))
            header_next = False
            continue
        if not isinstance(arg, str):
            redacted.append(arg)
            continue
        name, separator, value = arg.partition("=")
        if name in _SENSITIVE_FLAGS:
            redacted.append(f"{name}=<redacted>" if separator else name)
            redact_next = not separator
            continue
        if name == "--header":
            redacted.append(
                f"{name}={_redact_header_value(value)}" if separator else name
            )
            header_next = not separator
            continue
        redacted.append("<redacted>" if text_contains_secret(arg) else arg)
    return redacted


def _redact_header_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    name, separator, _header_value = value.partition(":")
    if separator and name.strip().lower() in _SENSITIVE_HEADER_NAMES:
        return f"{name.strip()}: <redacted>"
    return "<redacted>" if text_contains_secret(value) else value


def _is_sensitive_query_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_QUERY_KEYS or is_sensitive_key(normalized)
