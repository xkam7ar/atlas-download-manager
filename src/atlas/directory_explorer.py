"""First-class directory explorer state and valid action rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from atlas.directory_index import DirectoryEntry, DirectoryIndex
from atlas.directory_tree import DirectoryTree
from atlas.models import ScanStatus


class DirectoryExplorerAction(StrEnum):
    everything = "everything"
    open_folder = "folder"
    # Compatibility alias for callers that persisted the original action value.
    folder = "folder"
    folders = "folders"
    visible_files = "visible_files"
    tree = "tree"
    deep_scan = "deep_scan"
    offline_site = "offline_site"
    back = "back"
    quit = "quit"


@dataclass(frozen=True)
class DirectoryExplorerState:
    """The safe visible state before Atlas commits to a recursive scan."""

    index: DirectoryIndex
    status: ScanStatus

    @property
    def tree(self) -> DirectoryTree:
        return DirectoryTree.from_index(self.index)

    @property
    def actions(self) -> tuple[DirectoryExplorerAction, ...]:
        return directory_explorer_actions(self.index, status=self.status)


def directory_explorer_actions(
    index: DirectoryIndex,
    *,
    status: ScanStatus,
) -> tuple[DirectoryExplorerAction, ...]:
    """Return only actions that make sense for the current scan state."""

    if status == ScanStatus.failed:
        return (DirectoryExplorerAction.back, DirectoryExplorerAction.quit)

    actions: list[DirectoryExplorerAction] = []
    safe_folders = _same_origin_folders(index)
    if safe_folders:
        actions.append(DirectoryExplorerAction.open_folder)
    if _has_downloadable_visible_files(index):
        actions.append(DirectoryExplorerAction.visible_files)
    if safe_folders or _has_downloadable_visible_files(index):
        actions.append(DirectoryExplorerAction.everything)
    if safe_folders:
        actions.extend(
            [
                DirectoryExplorerAction.folders,
                DirectoryExplorerAction.tree,
                DirectoryExplorerAction.deep_scan,
            ]
        )
    actions.extend(
        [
            DirectoryExplorerAction.offline_site,
            DirectoryExplorerAction.back,
            DirectoryExplorerAction.quit,
        ]
    )
    return tuple(actions)


def _has_downloadable_visible_files(index: DirectoryIndex) -> bool:
    return any(
        entry.kind == "file" and not entry.parent and _entry_is_same_origin(index, entry.url)
        for entry in index.files
    )


def _same_origin_folders(index: DirectoryIndex) -> tuple[DirectoryEntry, ...]:
    return tuple(entry for entry in index.folders if _entry_is_same_origin(index, entry.url))


def _entry_is_same_origin(index: DirectoryIndex, url: str) -> bool:
    source_origin = _origin(index.source_url)
    return source_origin is not None and _origin(url) == source_origin


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    return scheme, parsed.hostname.lower(), port
