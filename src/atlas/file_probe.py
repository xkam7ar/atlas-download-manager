"""Direct-file HTTP metadata probing."""

from __future__ import annotations

import re
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from atlas.models import DirectFileProbe
from atlas.network import FetchClient, FetchError, FetchErrorCode, FetchOptions, FetchResponse
from atlas.paths import safe_filename

_CONTENT_RANGE_TOTAL = re.compile(r"/(?P<total>\d+|\*)$")
_FALLBACK_HEAD_STATUS_CODES = {403, 405, 501}
_HTML_EXTENSIONS = {".html", ".htm", ".shtml", ".xhtml", ".php", ".asp", ".aspx", ".jsp"}
_DIRECT_FILE_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bz2",
    ".csv",
    ".dmg",
    ".epub",
    ".flac",
    ".gz",
    ".iso",
    ".jpg",
    ".jpeg",
    ".json",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".pdf",
    ".png",
    ".rar",
    ".tar",
    ".tbz2",
    ".tgz",
    ".txt",
    ".txz",
    ".wav",
    ".webm",
    ".xz",
    ".zip",
    ".zst",
}
_ARCHIVE_LABELS = {
    ".7z": "7Z",
    ".bz2": "BZIP2",
    ".dmg": "DMG",
    ".gz": "GZIP",
    ".iso": "ISO",
    ".rar": "RAR",
    ".tar": "TAR",
    ".tbz2": "TAR",
    ".tgz": "TAR",
    ".txz": "TAR",
    ".xz": "XZ",
    ".zip": "ZIP",
    ".zst": "Zstandard archive",
}
_SHORTLINK_HOSTS = {
    "bit.ly",
    "buff.ly",
    "cutt.ly",
    "goo.gl",
    "is.gd",
    "ow.ly",
    "rebrand.ly",
    "t.co",
    "tinyurl.com",
}
_TRACKING_QUERY_PARAMS = {
    "_ga",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "ref_src",
    "spm",
    "twclid",
    "vero_id",
    "yclid",
}
_SIGNED_QUERY_PARAMS = {
    "expires",
    "key-pair-id",
    "policy",
    "signature",
    "token",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-goog-algorithm",
    "x-goog-credential",
    "x-goog-date",
    "x-goog-expires",
    "x-goog-signature",
}


def probe_direct_file(url: str, *, timeout: float = 10.0) -> DirectFileProbe:
    """Probe a direct-file URL without downloading the body."""

    try:
        return _probe_with_method(url, method="HEAD", timeout=timeout)
    except FetchError as exc:
        if (
            exc.failure.code != FetchErrorCode.http_error
            or exc.failure.status_code not in _FALLBACK_HEAD_STATUS_CODES
        ):
            return _probe_error(url, exc)

    try:
        return _probe_with_method(
            url,
            method="GET",
            timeout=timeout,
            extra_headers={"Range": "bytes=0-0"},
        )
    except FetchError as exc:
        return _probe_error(url, exc)


def unprobed_direct_file(url: str, *, reason: str) -> DirectFileProbe:
    """Return an explicit non-network probe result."""

    host = _host(url)
    fingerprint = url_fingerprint(url)
    return DirectFileProbe(
        url=url,
        final_url=url,
        file_extension=_extension_from_url(url),
        host=host,
        final_host=host,
        robots_url=_robots_url(url),
        url_fingerprint=fingerprint,
        mirror_fingerprint=fingerprint,
        classification_notes=_classification_notes_for_unprobed(url, reason=reason),
        warning_flags=_warning_flags_for_url(url),
        probed=False,
        error=reason,
    )


def _probe_with_method(
    url: str,
    *,
    method: str,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
) -> DirectFileProbe:
    response = FetchClient().request(
        url,
        FetchOptions(timeout=timeout, user_agent="atlas/0.1"),
        method=method,
        extra_headers=extra_headers,
        body_limit=0,
    )
    response_headers = response.headers
    link_headers = _message_from_headers(response)
    final_url = response.final_url
    status_code = response.status_code
    host = _host(url)
    final_host = _host(final_url)
    redirected = bool(final_url and final_url != url)
    external_host = bool(host and final_host and host != final_host)
    content_disposition = response_headers.get("Content-Disposition")
    filename = _filename_from_content_disposition(content_disposition)
    content_range = response_headers.get("Content-Range")
    content_length = _content_length(
        response_headers.get("Content-Length"),
        content_range=content_range,
    )
    accept_ranges = response_headers.get("Accept-Ranges")
    supports_ranges = (
        (accept_ranges or "").lower() == "bytes" or bool(content_range) or status_code == 206
    )
    extension = _extension_from_filename(filename) or _extension_from_url(final_url or url)
    classification_notes = _classification_notes(
        original_url=url,
        final_url=final_url or url,
        content_type=response_headers.get("Content-Type"),
        filename=filename,
        extension=extension,
    )
    warning_flags = _warning_flags_for_url(url)
    fingerprint = url_fingerprint(final_url or url)
    mirror_fingerprint = _mirror_fingerprint(
        final_url=final_url or url,
        filename=filename,
        extension=extension,
        content_length=content_length,
        etag=response_headers.get("ETag"),
    )
    linked_metalink = _metalink_from_link_headers(
        link_headers,
        base_url=final_url or url,
    )
    return DirectFileProbe(
        url=url,
        final_url=final_url,
        redirected=redirected,
        content_type=response_headers.get("Content-Type"),
        content_length=content_length,
        content_disposition=content_disposition,
        filename=filename,
        accept_ranges=accept_ranges,
        supports_ranges=supports_ranges,
        etag=response_headers.get("ETag"),
        last_modified=response_headers.get("Last-Modified"),
        file_extension=extension,
        host=host,
        final_host=final_host,
        redirect_target=final_url if redirected else None,
        metalink_url=linked_metalink[0] if linked_metalink else None,
        metalink_source=linked_metalink[1] if linked_metalink else None,
        robots_url=_robots_url(final_url or url),
        url_fingerprint=fingerprint,
        mirror_fingerprint=mirror_fingerprint,
        classification_notes=classification_notes,
        warning_flags=warning_flags,
        same_host=not external_host,
        external_host=external_host,
    )


def _message_from_headers(response: FetchResponse) -> Message:
    message = Message()
    for key, value in response.headers.items():
        message[key] = value
    return message


def _content_length(value: str | None, *, content_range: str | None) -> int | None:
    if content_range:
        match = _CONTENT_RANGE_TOTAL.search(content_range.strip())
        if match and match.group("total") != "*":
            return int(match.group("total"))
    if value and value.isdigit():
        return int(value)
    return None


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename_value = message.get_param("filename*", header="content-disposition")
    if filename_value is None:
        filename_value = message.get_param("filename", header="content-disposition")
    if filename_value is None:
        return None
    collapsed = collapse_rfc2231_value(filename_value)
    return _safe_filename(collapsed)


def _metalink_from_link_headers(headers: Message, *, base_url: str) -> tuple[str, str] | None:
    for raw_header in headers.get_all("Link", []):
        for link_value in _split_link_header(raw_header):
            parsed = _parse_link_value(link_value)
            if parsed is None:
                continue
            target, params = parsed
            rels = {
                rel.strip().lower()
                for rel in params.get("rel", "").replace(",", " ").split()
                if rel.strip()
            }
            if not ({"describedby", "duplicate"} & rels):
                continue
            media_type = params.get("type", "").lower()
            absolute = urljoin(base_url, target)
            if "metalink" in media_type or _extension_from_url(absolute) in {
                ".meta4",
                ".metalink",
            }:
                return absolute, next(iter({"describedby", "duplicate"} & rels))
    return None


def _split_link_header(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_quote = False
    angle_depth = 0
    for index, char in enumerate(value):
        if char == '"' and (index == 0 or value[index - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if char == "<":
                angle_depth += 1
            elif char == ">" and angle_depth:
                angle_depth -= 1
            elif char == "," and angle_depth == 0:
                part = value[start:index].strip()
                if part:
                    parts.append(part)
                start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_link_value(value: str) -> tuple[str, dict[str, str]] | None:
    cleaned = value.strip()
    if not cleaned.startswith("<"):
        return None
    end = cleaned.find(">")
    if end <= 1:
        return None
    target = cleaned[1:end].strip()
    params: dict[str, str] = {}
    for raw_param in cleaned[end + 1 :].split(";"):
        if "=" not in raw_param:
            continue
        name, raw_value = raw_param.split("=", 1)
        key = name.strip().lower()
        if not key:
            continue
        params[key] = raw_value.strip().strip('"').strip("'")
    return target, params


def _safe_filename(value: str) -> str | None:
    name = safe_filename(value, default="")
    return name or None


def _extension_from_filename(value: str | None) -> str | None:
    if not value:
        return None
    return PurePosixPath(value).suffix.lower() or None


def _extension_from_url(url: str) -> str | None:
    return PurePosixPath(urlparse(url).path).suffix.lower() or None


def url_fingerprint(url: str) -> str:
    """Return a conservative URL identity for duplicate detection."""

    parsed = urlparse(url)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    path = parsed.path or "/"
    normalized = parsed._replace(fragment="", path=path, query=query)
    return urlunparse(normalized)


def _is_tracking_param(key: str) -> bool:
    normalized = key.lower()
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_PARAMS


def _warning_flags_for_url(url: str) -> list[str]:
    parsed = urlparse(url)
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    flags: list[str] = []
    if any(_is_tracking_param(key) for key in query_keys):
        flags.append("tracking_params")
    if query_keys & _SIGNED_QUERY_PARAMS:
        flags.append("signed_query_params")
    return flags


def _classification_notes(
    *,
    original_url: str,
    final_url: str,
    content_type: str | None,
    filename: str | None,
    extension: str | None,
) -> list[str]:
    notes: list[str] = []
    original_extension = _extension_from_url(original_url)
    final_extension = _extension_from_url(final_url)
    original_parsed = urlparse(original_url)
    final_parsed = urlparse(final_url)
    original_looks_page = _looks_like_page_url(original_url)
    original_looks_file = _looks_like_file_url(original_url)

    if original_parsed.scheme == "http" and final_parsed.scheme == "https":
        notes.append("Redirected from HTTP to HTTPS.")
    if _is_shortlink_host(original_parsed.hostname) and final_url != original_url:
        notes.append("Shortlink resolved before planning.")
    if original_looks_page and extension and extension not in _HTML_EXTENSIONS:
        notes.append(f"This looked like a page, but resolved to a {_extension_label(extension)}.")
    if original_looks_file and _content_type_is_html(content_type):
        notes.append("This looked like a file, but returned HTML.")
    if not original_extension and filename and extension:
        notes.append(f"No extension in URL, but Content-Disposition named {filename}.")
    if final_extension and original_extension and final_extension != original_extension:
        notes.append(
            f"Redirect changed file extension from {original_extension} to {final_extension}."
        )
    if "tracking_params" in _warning_flags_for_url(original_url):
        notes.append("Tracking parameters ignored for URL dedupe fingerprint.")
    if "signed_query_params" in _warning_flags_for_url(original_url):
        notes.append(
            "Signed query parameters detected; original URL is preserved "
            "and dedupe stays conservative."
        )
    return _dedupe(notes)


def _classification_notes_for_unprobed(url: str, *, reason: str) -> list[str]:
    notes: list[str] = []
    if "tracking_params" in _warning_flags_for_url(url):
        notes.append("Tracking parameters ignored for URL dedupe fingerprint.")
    if "signed_query_params" in _warning_flags_for_url(url):
        notes.append(
            "Signed query parameters detected; original URL is preserved "
            "and dedupe stays conservative."
        )
    if reason:
        notes.append(f"Probe skipped: {reason}")
    return notes


def _mirror_fingerprint(
    *,
    final_url: str,
    filename: str | None,
    extension: str | None,
    content_length: int | None,
    etag: str | None,
) -> str:
    if filename or content_length is not None or etag:
        safe_name = safe_filename(
            filename or PurePosixPath(urlparse(final_url).path).name,
            default="unknown",
        )
        return ":".join(
            [
                "file",
                safe_name,
                str(content_length) if content_length is not None else "?",
                etag or "?",
            ]
        )
    if extension:
        return f"{url_fingerprint(final_url)}#{extension}"
    return url_fingerprint(final_url)


def _looks_like_page_url(url: str) -> bool:
    extension = _extension_from_url(url)
    if not extension:
        return True
    return extension in _HTML_EXTENSIONS


def _looks_like_file_url(url: str) -> bool:
    extension = _extension_from_url(url)
    if extension in _DIRECT_FILE_EXTENSIONS:
        return True
    parsed = urlparse(url)
    query_values = [value for _key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    return any(_extension_from_url(value) in _DIRECT_FILE_EXTENSIONS for value in query_values)


def _content_type_is_html(content_type: str | None) -> bool:
    return bool(content_type and "html" in content_type.lower())


def _extension_label(extension: str) -> str:
    return _ARCHIVE_LABELS.get(extension, f"{extension.lstrip('.').upper()} file")


def _is_shortlink_host(host: str | None) -> bool:
    return bool(host and host.lower().removeprefix("www.") in _SHORTLINK_HOSTS)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname


def _robots_url(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _probe_error(url: str, exc: BaseException) -> DirectFileProbe:
    host = _host(url)
    fingerprint = url_fingerprint(url)
    return DirectFileProbe(
        url=url,
        final_url=url,
        file_extension=_extension_from_url(url),
        host=host,
        final_host=host,
        robots_url=_robots_url(url),
        url_fingerprint=fingerprint,
        mirror_fingerprint=fingerprint,
        classification_notes=_classification_notes_for_unprobed(url, reason=str(exc)),
        warning_flags=_warning_flags_for_url(url),
        probed=False,
        error=str(exc),
    )
