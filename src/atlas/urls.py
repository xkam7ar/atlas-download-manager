"""URL classification helpers."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

_METALINK_EXTENSIONS = {".meta4", ".metalink"}


def is_explicit_playlist_url(url: str) -> bool:
    """Return true only for URLs that point at a playlist page itself."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        return bool(query.get("list")) and path in {"/playlist", "/embed/videoseries"}

    if "youtu.be" in host:
        return False

    if "rumble.com" in host:
        return "playlist" in path or "playlists" in path

    return "playlist" in path or "playlists" in path


def is_watch_url_with_playlist_params(url: str) -> bool:
    """Return true for single-watch URLs that carry playlist/radio query params."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)
    has_playlist_context = bool(query.get("list") or query.get("start_radio"))

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        return has_playlist_context and path == "/watch"

    if "youtu.be" in host:
        return has_playlist_context

    return False


def is_metalink_url(url: str) -> bool:
    """Return true when a URL path points at a Metalink manifest."""

    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(extension) for extension in _METALINK_EXTENSIONS)
