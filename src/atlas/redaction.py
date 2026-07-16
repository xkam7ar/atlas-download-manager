"""Small, shared helpers for keeping credentials out of human and JSON output."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "authentication",
        "api_key",
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
_SENSITIVE_EXACT_KEYS = frozenset({"auth", "jwt", "session", "ticket"})
_SENSITIVE_HEADER_NAMES = frozenset({"authorization", "cookie", "proxy-authorization"})
_SENSITIVE_HEADER_SEGMENTS = frozenset({"auth", "jwt", "session", "ticket"})
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "client_secret",
        "code",
        "id_token",
        "key_pair_id",
        "policy",
        "po_token",
        "refresh_token",
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
_SENSITIVE_QUERY_KEY_COMPACT = frozenset(
    {
        "accesstoken",
        "apikey",
        "auth",
        "awsaccesskeyid",
        "clientsecret",
        "idtoken",
        "jwt",
        "keypairid",
        "policy",
        "potoken",
        "refreshtoken",
        "session",
        "sessionid",
        "sharedaccesssignature",
        "sig",
        "signature",
        "ticket",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogcredential",
        "xgoogsignature",
    }
)
_SENSITIVE_FLAGS = frozenset(
    {
        "-p",
        "-u",
        "--all-proxy-passwd",
        "--all-proxy-user",
        "--ap-password",
        "--ap-username",
        "--body-data",
        "--body-file",
        "--client-certificate-key",
        "--client-certificate-password",
        "--cookie",
        "--cookies",
        "--ftp-password",
        "--ftp-passwd",
        "--ftp-user",
        "--geo-verification-proxy",
        "--http-password",
        "--http-passwd",
        "--http-user",
        "--https-passwd",
        "--https-user",
        "--load-cookies",
        "--netrc-file",
        "--netrc-location",
        "--password",
        "--post-data",
        "--post-file",
        "--private-key",
        "--proxy",
        "--proxy-password",
        "--proxy-user",
        "--rpc-passwd",
        "--rpc-secret",
        "--rpc-user",
        "--save-cookies",
        "--user",
        "--username",
        "--video-password",
    }
)
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(?:^|[\s;,:{\[])[\"']?(?:[a-z0-9]+[_-])*(?:access[_-]?token|api[_-]?key|"
    r"auth(?:entication|orization)?|cookie|jwt|po[_-]?token|password|secret|ticket|token)"
    r"(?:[_-][a-z0-9]+)*[\"']?\s*[:=]"
)
_SENSITIVE_FLAG_TEXT_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:"
    + "|".join(re.escape(flag) for flag in sorted(_SENSITIVE_FLAGS, key=len, reverse=True))
    + r")(?:=|\s+)\S+"
)
_SENSITIVE_ATTACHED_SHORT_PATTERN = re.compile(r"(?i)(?:^|\s)(?:-p|-u)\S+")
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
_ANSI_CSI_PATTERN = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ANSI_OSC_PATTERN = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b]*(?:\x07|\x1b\\)?")
_TERMINAL_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BIDI_CONTROL_PATTERN = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def is_sensitive_key(key: str) -> bool:
    """Return whether a structured field name can contain credential material."""

    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_EXACT_KEYS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )


def is_sensitive_header(name: str) -> bool:
    """Return whether a request header should never follow a redirect implicitly."""

    normalized = name.strip().lower()
    segments = re.split(r"[-_\s]+", normalized)
    return (
        normalized in _SENSITIVE_HEADER_NAMES
        or any(segment in _SENSITIVE_HEADER_SEGMENTS for segment in segments)
        or is_sensitive_key(normalized)
    )


def text_contains_secret(value: str) -> bool:
    """Detect credential-bearing URLs and common key/value secret forms."""

    if (
        _SENSITIVE_TEXT_PATTERN.search(value)
        or _SENSITIVE_FLAG_TEXT_PATTERN.search(value)
        or _SENSITIVE_ATTACHED_SHORT_PATTERN.search(value)
    ):
        return True
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    return any(
        _is_sensitive_query_key(name)
        for query in (parsed.query, _fragment_query(parsed.fragment))
        for name, _item in parse_qsl(query)
    )


def redact_url(value: str) -> str:
    """Redact credentials and signed query values while retaining URL context."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    query = _redact_query(parsed.query)
    fragment = _redact_fragment(parsed.fragment)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def redact_text(value: str) -> str:
    """Redact URL and key/value secrets from an arbitrary log message."""

    def replace_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        base = candidate.rstrip(_TRAILING_URL_PUNCTUATION)
        trailing = candidate[len(base) :]
        return f"{redact_url(base)}{trailing}"

    redacted = _URL_PATTERN.sub(replace_url, value)
    if (
        _SENSITIVE_TEXT_PATTERN.search(redacted)
        or _SENSITIVE_FLAG_TEXT_PATTERN.search(redacted)
        or _SENSITIVE_ATTACHED_SHORT_PATTERN.search(redacted)
    ):
        return "<redacted>"
    return redacted


def sanitize_terminal_text(value: str) -> str:
    """Remove terminal escape/control and bidi-override sequences from untrusted text."""

    without_osc = _ANSI_OSC_PATTERN.sub("", value)
    without_csi = _ANSI_CSI_PATTERN.sub("", without_osc)
    without_controls = _TERMINAL_CONTROL_PATTERN.sub("", without_csi)
    return _BIDI_CONTROL_PATTERN.sub("", without_controls)


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
        if len(arg) > 2 and arg[:2] in {"-p", "-u"}:
            redacted.append(f"{arg[:2]}<redacted>")
            continue
        name, separator, value = arg.partition("=")
        if name in _SENSITIVE_FLAGS or (
            name.startswith("--") and is_sensitive_key(name.removeprefix("--"))
        ):
            redacted.append(f"{name}=<redacted>" if separator else name)
            redact_next = not separator
            continue
        if name == "--header":
            redacted.append(f"{name}={_redact_header_value(value)}" if separator else name)
            header_next = not separator
            continue
        redacted.append("<redacted>" if text_contains_secret(arg) else arg)
    return redacted


def redact_structure(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secrets in JSON-compatible structured output."""

    if key is not None and is_sensitive_key(key):
        if value in (None, "", (), [], {}):
            return value
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_structure(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _redact_header_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    name, separator, _header_value = value.partition(":")
    normalized_name = name.strip().lower()
    if separator and is_sensitive_header(normalized_name):
        return f"{name.strip()}: <redacted>"
    return "<redacted>" if text_contains_secret(value) else value


def _is_sensitive_query_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return (
        normalized in _SENSITIVE_QUERY_KEYS
        or compact in _SENSITIVE_QUERY_KEY_COMPACT
        or is_sensitive_key(normalized)
    )


def _redact_query(query: str) -> str:
    return urlencode(
        [
            (name, "<redacted>" if _is_sensitive_query_key(name) else item)
            for name, item in parse_qsl(query, keep_blank_values=True)
        ],
        doseq=True,
        safe="<>",
    )


def _fragment_query(fragment: str) -> str:
    """Return the query-shaped part of an OAuth-style URL fragment."""

    return fragment.partition("?")[2] if "?" in fragment else fragment


def _redact_fragment(fragment: str) -> str:
    if not fragment:
        return fragment
    prefix, separator, query = fragment.partition("?")
    if separator:
        return f"{prefix}?{_redact_query(query)}"
    if "=" not in fragment:
        return fragment
    return _redact_query(fragment)
