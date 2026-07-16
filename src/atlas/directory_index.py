"""Open-directory index parsing and explorer models."""

from __future__ import annotations

import codecs
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

from atlas.models import HubKind, WorkItem
from atlas.redaction import sanitize_terminal_text

DirectoryEntryKind = Literal["directory", "file", "html", "unknown"]


@dataclass(frozen=True)
class DirectoryEntry:
    """One visible row from an HTTP directory index."""

    name: str
    url: str
    kind: DirectoryEntryKind
    parent: bool = False
    skipped_reason: str | None = None
    last_modified: datetime | None = None
    visible_size: int | None = None
    extension: str | None = None
    content_type: str | None = None
    depth: int = 0


@dataclass(frozen=True)
class DirectoryIndex:
    """A shallow, visible folder map for a directory-like URL."""

    source_url: str
    host: str | None
    entries: tuple[DirectoryEntry, ...]
    parser_name: str = "html"
    complete: bool = True
    truncated_reason: str | None = None

    @property
    def folders(self) -> tuple[DirectoryEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.kind == "directory" and not entry.parent and entry.skipped_reason is None
        )

    @property
    def files(self) -> tuple[DirectoryEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.kind in {"file", "html", "unknown"}
            and not entry.parent
            and entry.skipped_reason is None
        )

    @property
    def parents(self) -> tuple[DirectoryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.parent)

    @property
    def skipped(self) -> tuple[DirectoryEntry, ...]:
        return tuple(
            entry for entry in self.entries if entry.parent or entry.skipped_reason is not None
        )


_ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>(?P<tail>[^<\r\n]*)",
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(
    r"""href\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))""",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_PATTERNS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%b-%d %H:%M",
    "%Y-%b-%d %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%y %H:%M",
)
_DATE_RE = re.compile(
    r"(?<!\d)("
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
    r"|"
    r"\d{4}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?"
    r"|"
    r"\d{2}-[A-Za-z]{3}-\d{2,4}\s+\d{2}:\d{2}(?::\d{2})?"
    r")(?!\d)"
)
_SIZE_RE = re.compile(
    r"^(?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<unit>[KMGTPE]?)(?:i?B?)?$",
    re.I,
)
_SPLIT_SIZE_UNIT_RE = re.compile(r"^[KMGTPE]?(?:i?B)$", re.IGNORECASE)
_AUTOINDEX_HEADING_RE = re.compile(
    r"<(?:title|h1)\b[^>]*>\s*(?:index\s+of|directory\s+listing\s+for)\b",
    re.IGNORECASE | re.DOTALL,
)
_AUTOINDEX_TABLE_RE = re.compile(
    r"<table\b[^>]*\bid\s*=\s*(?:\"list\"|'list'|list(?:\s|>))",
    re.IGNORECASE,
)
_HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".shtml")
_DIRECTORY_QUERY_KEYS = frozenset({"dir", "directory", "folder"})
_MAX_DIRECTORY_ENTRIES = 2_000
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CHARSET_RE = re.compile(
    r"(?:^|;)\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^;\s]+))",
    re.IGNORECASE,
)
_IGNORED_HTML_RE = re.compile(
    r"<!--.*?(?:-->|$)|"
    r"<(?P<raw_text>script|style|template|textarea|xmp)\b[^>]*>.*?"
    r"(?:</(?P=raw_text)\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_COPYPARTY_ROW_RE = re.compile(
    r"^\s*(?P<timestamp>\d{14}|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<size>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[KMGTPE]?(?:i?B)?)\s+"
    r"(?P<directory>##\s+)?(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


class UnsupportedDirectoryIndexError(ValueError):
    """Raised when a textual response is not a recognized directory index."""


def decode_directory_body(body: bytes | str, content_type: str | None = None) -> str:
    """Decode a directory response using its declared charset when available."""

    if isinstance(body, str):
        return body
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if body.startswith(marker):
            return body.decode(encoding, errors="replace")
    match = _CHARSET_RE.search(content_type or "")
    declared = next((value for value in match.groups() if value), None) if match else None
    if declared:
        try:
            return body.decode(declared.strip(), errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")


def http_url_origin(url: str) -> tuple[str, str, int] | None:
    """Return normalized HTTP origin, including effective default port."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        normalized_host = (
            host.casefold() if ":" in host else host.encode("idna").decode("ascii").casefold()
        ).rstrip(".")
    except UnicodeError:
        return None
    return scheme, normalized_host, port or (443 if scheme == "https" else 80)


def same_http_origin(left: str, right: str) -> bool:
    left_origin = http_url_origin(left)
    return left_origin is not None and left_origin == http_url_origin(right)


def resolve_directory_href(base_url: str, href: str) -> str | None:
    """Resolve one href to a canonical, fragment-free HTTP(S) URL."""

    raw = unescape(href).strip()
    if (
        not raw
        or raw.startswith("#")
        or "\\" in raw
        or "\ufffd" in raw
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        return None
    try:
        parsed = urlsplit(urljoin(base_url, raw))
        port = parsed.port
    except ValueError:
        return None
    origin = http_url_origin(parsed.geturl())
    if origin is None or parsed.username is not None or parsed.password is not None:
        return None
    scheme, host, _effective_port = origin
    default_port = 443 if scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="!$&'()*+,;=:/?@%-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def same_http_resource(left: str, right: str) -> bool:
    left_url = resolve_directory_href(left, left)
    right_url = resolve_directory_href(right, right)
    if left_url is None or right_url is None:
        return False
    left_parts = urlsplit(left_url)
    right_parts = urlsplit(right_url)
    return (
        http_url_origin(left_url),
        left_parts.path or "/",
        left_parts.query,
    ) == (
        http_url_origin(right_url),
        right_parts.path or "/",
        right_parts.query,
    )


def is_directory_self_href(base_url: str, href: str, resolved_url: str) -> bool:
    """Identify path-only links back to the current directory, without hiding query navigation."""

    raw = unescape(href).strip()
    try:
        href_parts = urlsplit(raw)
        base_parts = urlsplit(base_url)
        resolved_parts = urlsplit(resolved_url)
    except ValueError:
        return False
    if href_parts.query or not same_http_origin(base_url, resolved_url):
        return False
    base_path = _canonical_scope_path(base_parts.path)
    resolved_path = _canonical_scope_path(resolved_parts.path)
    if base_path is None or resolved_path is None:
        return False
    return base_path.rstrip("/") == resolved_path.rstrip("/")


def url_within_directory_scope(seed_url: str, candidate_url: str) -> bool:
    """Return true when candidate remains inside seed's no-parent URL subtree."""

    if not same_http_origin(seed_url, candidate_url):
        return False
    seed_path = _canonical_scope_path(urlsplit(seed_url).path)
    candidate_path = _canonical_scope_path(urlsplit(candidate_url).path)
    if seed_path is None or candidate_path is None:
        return False
    seed_scope = _directory_scope(seed_path)
    return (
        seed_scope == "/"
        or candidate_path == seed_scope.rstrip("/")
        or candidate_path.startswith(seed_scope)
    )


def _canonical_scope_path(path: str) -> str | None:
    parts = safe_http_url_path_parts(path)
    if parts is None:
        return None
    trailing_slash = (path or "/").endswith("/")
    normalized = posixpath.normpath(f"/{'/'.join(parts)}")
    if trailing_slash and normalized != "/":
        normalized = f"{normalized}/"
    return normalized


def safe_http_url_path_parts(path: str) -> tuple[str, ...] | None:
    """Decode URL path components while rejecting ambiguous filesystem separators."""

    parts: list[str] = []
    for raw_part in (path or "/").split("/"):
        if not raw_part:
            continue
        if re.search(r"%(?![0-9A-Fa-f]{2})", raw_part):
            return None
        part = raw_part
        for _ in range(8):
            if re.search(r"%[0-9A-Fa-f]{2}", part) is None:
                break
            decoded = unquote(part)
            if decoded == part:
                break
            part = decoded
        else:
            if re.search(r"%[0-9A-Fa-f]{2}", part):
                return None
        if (
            part in {".", ".."}
            or "/" in part
            or "\\" in part
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            return None
        parts.append(part)
    return tuple(parts)


def _directory_scope(path: str) -> str:
    if not path or path == "/":
        return "/"
    if path.endswith("/"):
        return path
    directory = path.rsplit("/", 1)[0]
    return f"{directory}/" if directory else "/"


def _mask_ignored_html(text: str) -> str:
    def mask(match: re.Match[str]) -> str:
        return "".join(char if char in "\r\n" else " " for char in match.group(0))

    return _IGNORED_HTML_RE.sub(mask, text)


def _entry_skip_reason(base_url: str, url: str, *, parent: bool) -> str | None:
    if not same_http_origin(base_url, url):
        return "external link skipped by default"
    if parent or not url_within_directory_scope(base_url, url):
        return "parent directory link skipped by no-parent policy"
    return None


def parse_directory_index(
    base_url: str,
    body: bytes | str,
    *,
    content_type: str | None = None,
    depth: int = 0,
) -> DirectoryIndex:
    """Parse supported HTML and CopyParty plain-text directory indexes."""

    text = decode_directory_body(body, content_type)
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if _looks_like_copyparty_text(text):
        return _parse_copyparty_text(base_url, text, depth=depth)
    copyparty_html = _looks_like_copyparty_html(text)
    autoindex_html = _looks_like_autoindex_html(text)
    if media_type and "html" not in media_type:
        if media_type.startswith("text/"):
            raise UnsupportedDirectoryIndexError(
                f"{media_type} response is not a recognized plain-text directory index"
            )
        return DirectoryIndex(
            source_url=base_url,
            host=_host(base_url),
            entries=(),
            parser_name="unsupported-content-type",
        )

    entries: list[DirectoryEntry] = []
    seen: set[str] = set()
    truncated = False
    searchable_text = _mask_ignored_html(text)
    for match in _ANCHOR_RE.finditer(searchable_text):
        href = _href_from_attrs(match.group("attrs"))
        if not href:
            continue
        url = resolve_directory_href(base_url, href)
        if url is None:
            continue
        label = _clean_label(match.group("label")) or _name_from_url(url)
        if _is_autoindex_sort_link(base_url, url, href=href, label=label):
            continue
        if copyparty_html and _is_copyparty_html_control(base_url, url, label=label):
            continue
        parent = _is_parent_entry(href, label)
        if not parent and (
            same_http_resource(base_url, url) or is_directory_self_href(base_url, href, url)
        ):
            continue
        if url in seen:
            continue
        if len(entries) >= _MAX_DIRECTORY_ENTRIES:
            truncated = True
            break
        seen.add(url)
        tail = _entry_tail(text, match.end(), match.group("tail") or "")
        visible_size = _parse_visible_size(tail)
        kind = _entry_kind(
            url,
            href=href,
            label=label,
            parent=parent,
            visible_size=visible_size,
        )
        display_label = _directory_query_name(url) or label
        entries.append(
            DirectoryEntry(
                name=_display_name(display_label, url, kind=kind, parent=parent),
                url=url,
                kind=kind,
                parent=parent,
                skipped_reason=_entry_skip_reason(base_url, url, parent=parent),
                last_modified=_parse_last_modified(tail),
                visible_size=visible_size,
                extension=_extension_from_url(url),
                depth=depth,
            )
        )
    return DirectoryIndex(
        source_url=base_url,
        host=_host(base_url),
        entries=tuple(entries),
        parser_name=(
            "copyparty-html" if copyparty_html else "autoindex-html" if autoindex_html else "html"
        ),
        complete=not truncated,
        truncated_reason="entry-limit" if truncated else None,
    )


def _looks_like_copyparty_text(text: str) -> bool:
    plain = _ANSI_ESCAPE_RE.sub("", text)
    headers = {line.partition(":")[0].strip().lower() for line in plain.splitlines()[:12]}
    return "# perms" in headers and bool(headers & {"# acct", "# srvinf"})


def _looks_like_copyparty_html(text: str) -> bool:
    sample = text[:16_384].lower()
    return 'id="ht_brw"' in sample and "/.cpr/w/" in sample


def _looks_like_autoindex_html(text: str) -> bool:
    sample = text[:65_536]
    return bool(_AUTOINDEX_HEADING_RE.search(sample) or _AUTOINDEX_TABLE_RE.search(sample))


def _is_copyparty_html_control(base_url: str, url: str, *, label: str) -> bool:
    normalized_label = label.strip().casefold().rstrip("/")
    if normalized_label in {"zip", "switch to basic browser", "control-panel"}:
        return True
    base = urlparse(base_url)
    parsed = urlparse(url)
    return same_http_origin(base_url, url) and parsed.path.rstrip("/") == base.path.rstrip("/")


def _parse_copyparty_text(base_url: str, text: str, *, depth: int) -> DirectoryIndex:
    entries: list[DirectoryEntry] = []
    seen: set[str] = set()
    truncated = False
    for raw_line in _ANSI_ESCAPE_RE.sub("", text).splitlines():
        match = _COPYPARTY_ROW_RE.match(raw_line)
        if match is None:
            continue
        timestamp = match.group("timestamp")
        legacy_directory_marker = match.group("directory") if timestamp.isdigit() else None
        name = (
            f"{match.group('directory') or ''}{match.group('name')}"
            if not timestamp.isdigit()
            else match.group("name")
        ).strip()
        is_directory = bool(legacy_directory_marker) or name.endswith("/")
        if not _safe_plain_entry_name(name, is_directory=is_directory):
            continue
        url = _plain_entry_url(base_url, name)
        if url in seen:
            continue
        if len(entries) >= _MAX_DIRECTORY_ENTRIES:
            truncated = True
            break
        seen.add(url)
        kind: DirectoryEntryKind = (
            "directory"
            if is_directory
            else _entry_kind(
                url,
                href=name,
                label=name,
                parent=False,
                visible_size=_size_token_to_bytes(match.group("size").replace(",", "")),
            )
        )
        entries.append(
            DirectoryEntry(
                name=_display_name(name, url, kind=kind, parent=False),
                url=url,
                kind=kind,
                last_modified=_parse_copyparty_timestamp(timestamp),
                visible_size=_size_token_to_bytes(match.group("size").replace(",", "")),
                extension=_extension_from_url(url),
                depth=depth,
            )
        )
    if not entries:
        raise UnsupportedDirectoryIndexError(
            "CopyParty text headers were present, but no safe directory rows were recognized"
        )
    return DirectoryIndex(
        source_url=base_url,
        host=_host(base_url),
        entries=tuple(entries),
        parser_name="copyparty-text",
        complete=not truncated,
        truncated_reason="entry-limit" if truncated else None,
    )


def _safe_plain_entry_name(name: str, *, is_directory: bool) -> bool:
    if (
        not name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or "\ufffd" in name
        or any(ord(char) < 32 for char in name)
    ):
        return False
    normalized = name[:-1] if is_directory and name.endswith("/") else name
    parts = normalized.replace("\\", "/").split("/")
    return bool(normalized) and all(part not in {"", ".", ".."} for part in parts)


def _plain_entry_url(base_url: str, name: str) -> str:
    parsed = urlparse(base_url)
    folder_url = parsed._replace(
        path=f"{parsed.path.rstrip('/')}/",
        params="",
        query="",
        fragment="",
    ).geturl()
    encoded = quote(name, safe="/-._~!$&'()*+,;=:@")
    resolved = resolve_directory_href(folder_url, encoded)
    if resolved is None:  # guarded by _safe_plain_entry_name and a valid scan base URL
        raise UnsupportedDirectoryIndexError("CopyParty row resolved to an unsafe URL")
    return resolved


def _parse_copyparty_timestamp(value: str) -> datetime | None:
    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def directory_index_from_work_item(scan: WorkItem) -> DirectoryIndex:
    """Build an explorer-friendly directory index from a scanned Atlas WorkItem."""

    base_url = scan.final_url or scan.url
    entries: list[DirectoryEntry] = []
    for item in scan.discovered_work_items:
        url = resolve_directory_href(base_url, item.url)
        if url is None:
            continue
        kind = _kind_from_work_item(item)
        parent = "parent directory entry" in item.classification_notes
        skipped_reason = item.error or _entry_skip_reason(base_url, url, parent=parent)
        display_name = item.filename or _directory_query_name(url) or _name_from_url(url)
        entries.append(
            DirectoryEntry(
                name=_display_name(display_name, url, kind=kind, parent=parent),
                url=url,
                kind=kind,
                parent=parent,
                skipped_reason=skipped_reason,
                last_modified=_parse_http_datetime(item.last_modified),
                visible_size=item.content_length,
                extension=item.file_extension or _extension_from_url(url),
                content_type=item.content_type,
                depth=item.recursion_depth or 0,
            )
        )
    if not entries:
        for link in scan.discovered_links:
            url = resolve_directory_href(base_url, link)
            if url is None or same_http_resource(base_url, url):
                continue
            parent = _is_parent_entry(link, _name_from_url(url))
            kind = _entry_kind(url, href=link, label=_name_from_url(url), parent=parent)
            entries.append(
                DirectoryEntry(
                    name=_display_name(_name_from_url(url), url, kind=kind, parent=parent),
                    url=url,
                    kind=kind,
                    parent=parent,
                    skipped_reason=_entry_skip_reason(base_url, url, parent=parent),
                    extension=_extension_from_url(url),
                    depth=1,
                )
            )
    return DirectoryIndex(
        source_url=base_url,
        host=scan.final_host or scan.host or _host(base_url),
        entries=tuple(entries),
        parser_name=(
            "copyparty-text" if scan.scan_type == "directory-style text index" else "scan"
        ),
        complete=scan.scan_counts.get("complete", 1) != 0,
        truncated_reason=(
            "body-limit"
            if scan.scan_counts.get("body_truncated")
            else "entry-limit"
            if scan.scan_counts.get("links_truncated")
            else None
        ),
    )


def _href_from_attrs(attrs: str) -> str | None:
    match = _HREF_RE.search(attrs)
    if match is None:
        return None
    return match.group("double") or match.group("single") or match.group("bare")


def _clean_label(value: str) -> str:
    return " ".join(safe_directory_display_name(unescape(_TAG_RE.sub("", value))).split()).strip()


def safe_directory_display_name(value: str) -> str:
    """Remove terminal control sequences while preserving ordinary Unicode names."""

    return sanitize_terminal_text(value)


def _entry_tail(text: str, start: int, fallback: str) -> str:
    row_end = text.find("</tr>", start)
    next_anchor = text.find("<a", start)
    if row_end != -1 and (next_anchor == -1 or row_end < next_anchor):
        return _clean_tail(text[start:row_end])
    return unescape(fallback or "")


def _clean_tail(value: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", value)).split()).strip()


def _is_autoindex_sort_link(base_url: str, url: str, *, href: str, label: str) -> bool:
    if not href.strip().startswith("?"):
        return False
    base = urlparse(base_url)
    parsed = urlparse(url)
    if parsed.path != base.path:
        return False
    normalized_label = label.strip().casefold()
    query = parsed.query.upper()
    sort_labels = {
        "name",
        "file name",
        "last modified",
        "date",
        "size",
        "file size",
        "description",
        "↑",
        "↓",
    }
    return normalized_label in sort_labels and "C=" in query and "O=" in query


def _display_name(
    label: str,
    url: str,
    *,
    kind: DirectoryEntryKind,
    parent: bool,
) -> str:
    if parent:
        return "Parent Directory"
    name = safe_directory_display_name(label.strip())
    if not name:
        name = safe_directory_display_name(_name_from_url(url))
    if not name:
        name = "unnamed"
    if kind == "directory" and not name.endswith("/"):
        return f"{name}/"
    return name


def _name_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    stripped = path.rstrip("/")
    if not stripped:
        return "/"
    return unquote(stripped.rsplit("/", 1)[-1])


def _is_parent_entry(href: str, label: str) -> bool:
    normalized = unescape(href).strip().casefold()
    return normalized in {"..", "../"} or "parent directory" in label.strip().lower()


def _directory_query_name(url: str) -> str | None:
    for key, value in reversed(parse_qsl(urlparse(url).query, keep_blank_values=False)):
        if key.casefold() not in _DIRECTORY_QUERY_KEYS:
            continue
        normalized = safe_directory_display_name(value).strip().replace("\\", "/").rstrip("/")
        if normalized:
            return normalized.rsplit("/", 1)[-1]
    return None


def _entry_kind(
    url: str,
    *,
    href: str,
    label: str,
    parent: bool,
    visible_size: int | None = None,
) -> DirectoryEntryKind:
    if parent:
        return "directory"
    if _directory_query_name(url) is not None:
        return "directory"
    path = urlparse(url).path
    if href.endswith("/") or path.endswith("/") or label.endswith("/"):
        return "directory"
    extension = _extension_from_url(url)
    if extension in _HTML_SUFFIXES:
        return "html"
    if extension:
        return "file"
    if visible_size is not None:
        return "file"
    return "unknown"


def _kind_from_work_item(item: WorkItem) -> DirectoryEntryKind:
    if item.kind == HubKind.dir or urlparse(item.url).path.endswith("/"):
        return "directory"
    if item.kind == HubKind.site or (item.file_extension or "").lower() in _HTML_SUFFIXES:
        return "html"
    if item.kind in {HubKind.file, HubKind.audio, HubKind.video, HubKind.manifest}:
        return "file"
    return "unknown"


def _extension_from_url(url: str) -> str | None:
    path = urlparse(url).path.lower()
    leaf = path.rsplit("/", 1)[-1]
    if "." not in leaf:
        return None
    return "." + leaf.rsplit(".", 1)[-1]


def _parse_last_modified(tail: str) -> datetime | None:
    match = _DATE_RE.search(" ".join(tail.split()))
    if match is None:
        return None
    value = match.group(1).replace("T", " ")
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def _parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _parse_visible_size(tail: str) -> int | None:
    tokens = [token.strip() for token in " ".join(tail.split()).split()]
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        cleaned = token.strip("()[]")
        if cleaned == "-":
            return None
        if ":" in cleaned or "-" in cleaned:
            continue
        if index > 0 and _SPLIT_SIZE_UNIT_RE.match(cleaned):
            combined = f"{tokens[index - 1].strip('()[]')}{cleaned}"
            parsed = _size_token_to_bytes(combined)
            if parsed is not None:
                return parsed
        parsed = _size_token_to_bytes(cleaned)
        if parsed is not None:
            return parsed
    return None


def _size_token_to_bytes(value: str) -> int | None:
    match = _SIZE_RE.match(value)
    if match is None:
        return None
    try:
        number = Decimal(match.group("value").replace(",", ""))
    except InvalidOperation:
        return None
    unit = match.group("unit").upper()
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
        "E": 1024**6,
    }[unit]
    return int(number * multiplier)


def _host(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).hostname
    return host.lower() if host else None
