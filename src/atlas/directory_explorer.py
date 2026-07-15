"""First-class directory explorer state and valid action rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from atlas.directory_index import DirectoryIndex
from atlas.directory_tree import DirectoryTree
from atlas.models import ScanStatus


class DirectoryExplorerAction(StrEnum):
    everything = "everything"
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

    actions: list[DirectoryExplorerAction] = [DirectoryExplorerAction.everything]
    if index.folders:
        actions.extend(
            [
                DirectoryExplorerAction.folder,
                DirectoryExplorerAction.folders,
            ]
        )
    if _has_downloadable_visible_files(index):
        actions.append(DirectoryExplorerAction.visible_files)
    actions.extend(
        [
            DirectoryExplorerAction.tree,
            DirectoryExplorerAction.deep_scan,
            DirectoryExplorerAction.offline_site,
            DirectoryExplorerAction.back,
            DirectoryExplorerAction.quit,
        ]
    )
    return tuple(actions)


def _has_downloadable_visible_files(index: DirectoryIndex) -> bool:
    return any(entry.kind in {"file", "unknown"} and not entry.parent for entry in index.files)
