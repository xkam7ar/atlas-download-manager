"""Open-directory index parsing and explorer models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Literal
from urllib.parse import unquote, urljoin, urlparse

from atlas.models import HubKind, WorkItem

DirectoryEntryKind = Literal["directory", "file", "html", "unknown"]


@dataclass(frozen=True)
class DirectoryEntry:
    """One visible row from an HTTP directory index."""

    name: str
    url: str
    kind: DirectoryEntryKind
    parent: bool = False
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

    @property
    def folders(self) -> tuple[DirectoryEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.kind == "directory" and not entry.parent
        )

    @property
    def files(self) -> tuple[DirectoryEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.kind in {"file", "html", "unknown"} and not entry.parent
        )

    @property
    def parents(self) -> tuple[DirectoryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.parent)


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
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%y %H:%M",
)
_DATE_RE = re.compile(
    r"("
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
    r"|"
    r"\d{2}-[A-Za-z]{3}-\d{2,4}\s+\d{2}:\d{2}(?::\d{2})?"
    r")"
)
_SIZE_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[KMGTPE]?)(?:i?B?)?$", re.I)
_HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".shtml")


def parse_directory_index(
    base_url: str,
    body: bytes | str,
    *,
    content_type: str | None = None,
    depth: int = 0,
) -> DirectoryIndex:
    """Parse common Apache/nginx/LiteSpeed/Caddy/simple href directory indexes."""

    if content_type and "html" not in content_type.lower():
        return DirectoryIndex(source_url=base_url, host=_host(base_url), entries=())
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    entries: list[DirectoryEntry] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(text):
        href = _href_from_attrs(match.group("attrs"))
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urljoin(base_url, unescape(href))
        label = _clean_label(match.group("label")) or _name_from_url(url)
        if _is_autoindex_sort_link(base_url, url, href=href, label=label):
            continue
        if url in seen:
            continue
        seen.add(url)
        tail = _entry_tail(text, match.end(), match.group("tail") or "")
        parent = _is_parent_entry(href, label)
        kind = _entry_kind(url, href=href, label=label, parent=parent)
        entries.append(
            DirectoryEntry(
                name=_display_name(label, url, kind=kind, parent=parent),
                url=url,
                kind=kind,
                parent=parent,
                last_modified=_parse_last_modified(tail),
                visible_size=_parse_visible_size(tail),
                extension=_extension_from_url(url),
                depth=depth,
            )
        )
    return DirectoryIndex(source_url=base_url, host=_host(base_url), entries=tuple(entries))


def directory_index_from_work_item(scan: WorkItem) -> DirectoryIndex:
    """Build an explorer-friendly directory index from a scanned Atlas WorkItem."""

    entries: list[DirectoryEntry] = []
    for item in scan.discovered_work_items:
        url = item.url
        kind = _kind_from_work_item(item)
        parent = item.error == "parent directory link skipped by no-parent policy"
        entries.append(
            DirectoryEntry(
                name=_display_name(_name_from_url(url), url, kind=kind, parent=parent),
                url=url,
                kind=kind,
                parent=parent,
                last_modified=_parse_http_datetime(item.last_modified),
                visible_size=item.content_length,
                extension=item.file_extension or _extension_from_url(url),
                content_type=item.content_type,
                depth=item.recursion_depth or 0,
            )
        )
    if not entries:
        for link in scan.discovered_links:
            kind = _entry_kind(link, href=link, label=_name_from_url(link), parent=False)
            entries.append(
                DirectoryEntry(
                    name=_display_name(_name_from_url(link), link, kind=kind, parent=False),
                    url=urljoin(scan.final_url or scan.url, link),
                    kind=kind,
                    extension=_extension_from_url(link),
                    depth=1,
                )
            )
    return DirectoryIndex(
        source_url=scan.final_url or scan.url,
        host=scan.final_host or scan.host or _host(scan.final_url or scan.url),
        entries=tuple(entries),
    )


def _href_from_attrs(attrs: str) -> str | None:
    match = _HREF_RE.search(attrs)
    if match is None:
        return None
    return match.group("double") or match.group("single") or match.group("bare")


def _clean_label(value: str) -> str:
    return " ".join(unescape(_TAG_RE.sub("", value)).split()).strip()


def _entry_tail(text: str, start: int, fallback: str) -> str:
    row_end = text.find("</tr>", start)
    next_anchor = text.find("<a", start)
    if row_end != -1 and (next_anchor == -1 or row_end < next_anchor):
        return _clean_tail(text[start:row_end])
    return unescape(fallback or "")


def _clean_tail(value: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", value)).split()).strip()


def _is_autoindex_sort_link(base_url: str, url: str, *, href: str, label: str) -> bool:
    if not href.startswith("?"):
        return False
    base = urlparse(base_url)
    parsed = urlparse(url)
    if parsed.path != base.path:
        return False
    normalized_label = label.strip().lower()
    query = parsed.query.upper()
    return normalized_label in {"name", "last modified", "size", "description"} and (
        "C=" in query and "O=" in query
    )


def _display_name(
    label: str,
    url: str,
    *,
    kind: DirectoryEntryKind,
    parent: bool,
) -> str:
    if parent:
        return "Parent Directory"
    name = label.strip() or _name_from_url(url)
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
    normalized = href.strip().lower()
    return (
        normalized in {"..", "../"}
        or normalized.startswith("../")
        or "parent directory" in label.strip().lower()
    )


def _entry_kind(
    url: str,
    *,
    href: str,
    label: str,
    parent: bool,
) -> DirectoryEntryKind:
    if parent:
        return "directory"
    path = urlparse(url).path
    if href.endswith("/") or path.endswith("/") or label.endswith("/"):
        return "directory"
    extension = _extension_from_url(url)
    if extension in _HTML_SUFFIXES:
        return "html"
    if extension:
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
    for token in reversed(tokens):
        cleaned = token.strip("()[]")
        if cleaned == "-":
            return None
        if ":" in cleaned or "-" in cleaned:
            continue
        parsed = _size_token_to_bytes(cleaned)
        if parsed is not None:
            return parsed
    return None


def _size_token_to_bytes(value: str) -> int | None:
    match = _SIZE_RE.match(value)
    if match is None:
        return None
    number = float(match.group("value"))
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
