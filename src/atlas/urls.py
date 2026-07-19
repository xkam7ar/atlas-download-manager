"""URL classification helpers."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import parse_qs, urlparse

_METALINK_EXTENSIONS = {".meta4", ".metalink"}


class YoutubeUrlKind(StrEnum):
    """Safety-relevant YouTube URL shapes understood by Atlas."""

    other = "other"
    single = "single"
    watch_playlist_context = "watch-playlist-context"
    playlist = "playlist"
    collection = "collection"


def _youtube_host_kind(host: str | None) -> str | None:
    normalized = (host or "").lower().rstrip(".")
    if normalized == "youtu.be" or normalized.endswith(".youtu.be"):
        return "short"
    if normalized == "youtube.com" or normalized.endswith(".youtube.com"):
        return "youtube"
    if normalized == "youtube-nocookie.com" or normalized.endswith(".youtube-nocookie.com"):
        return "youtube"
    return None


def classify_youtube_url(url: str) -> YoutubeUrlKind:
    """Classify single-item, playlist-context, and multi-item YouTube URLs."""

    parsed = urlparse(url)
    host_kind = _youtube_host_kind(parsed.hostname)
    if host_kind is None:
        return YoutubeUrlKind.other

    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)
    has_playlist_context = bool(query.get("list") or query.get("start_radio"))

    if host_kind == "short":
        if not path:
            return YoutubeUrlKind.other
        if has_playlist_context:
            return YoutubeUrlKind.watch_playlist_context
        return YoutubeUrlKind.single

    if bool(query.get("list")) and path in {"/playlist", "/embed/videoseries"}:
        return YoutubeUrlKind.playlist
    if path == "/watch":
        if has_playlist_context:
            return YoutubeUrlKind.watch_playlist_context
        return YoutubeUrlKind.single
    if path.startswith(("/shorts/", "/live/")):
        return YoutubeUrlKind.single
    if path.startswith("/embed/") and path != "/embed/videoseries":
        return YoutubeUrlKind.single

    segments = tuple(segment for segment in path.split("/") if segment)
    if not segments:
        return YoutubeUrlKind.collection
    first = segments[0]
    if first.startswith("@"):
        return YoutubeUrlKind.collection
    if first in {"channel", "c", "user"} and len(segments) >= 2:
        return YoutubeUrlKind.collection
    if first in {"feed", "hashtag", "results", "browse"}:
        return YoutubeUrlKind.collection
    return YoutubeUrlKind.other


def is_explicit_playlist_url(url: str) -> bool:
    """Return true only for URLs that point at a playlist page itself."""

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/").lower()

    if _youtube_host_kind(host) is not None:
        return classify_youtube_url(url) == YoutubeUrlKind.playlist

    if "rumble.com" in host:
        return "playlist" in path or "playlists" in path

    return "playlist" in path or "playlists" in path


def is_watch_url_with_playlist_params(url: str) -> bool:
    """Return true for single-watch URLs that carry playlist/radio query params."""

    return classify_youtube_url(url) == YoutubeUrlKind.watch_playlist_context


def is_youtube_collection_url(url: str) -> bool:
    """Return true for channel, feed, search, and similar multi-item YouTube URLs."""

    return classify_youtube_url(url) == YoutubeUrlKind.collection


def is_metalink_url(url: str) -> bool:
    """Return true when a URL path points at a Metalink manifest."""

    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(extension) for extension in _METALINK_EXTENSIONS)
