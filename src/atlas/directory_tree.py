"""Directory tree helpers for selected remote folder scopes."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from atlas.directory_index import DirectoryEntry, DirectoryIndex


@dataclass(frozen=True)
class DirectoryTree:
    """A shallow tree map shown before a deeper adaptive scan."""

    source_url: str
    folders: tuple[DirectoryEntry, ...]
    files: tuple[DirectoryEntry, ...]

    @classmethod
    def from_index(cls, index: DirectoryIndex) -> DirectoryTree:
        return cls(
            source_url=index.source_url,
            folders=index.folders,
            files=index.files,
        )

    def selected_roots(self, selected_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return selected_directory_roots(self.source_url, self.folders, selected_names)

    def render_lines(self, *, max_folders: int = 20) -> list[str]:
        root = self.source_url.rstrip("/") + "/"
        lines = [root]
        visible = self.folders[:max_folders]
        for index, entry in enumerate(visible):
            connector = "`--" if index == len(visible) - 1 else "|--"
            name = entry.name if entry.name.endswith("/") else f"{entry.name}/"
            lines.append(f"{connector} {name}")
        remaining = len(self.folders) - len(visible)
        if remaining > 0:
            lines.append(f"`-- + {remaining} more")
        if not self.folders:
            lines.append("`-- no visible folders")
        return lines


def selected_directory_roots(
    base_url: str,
    folders: tuple[DirectoryEntry, ...] | list[DirectoryEntry],
    selected_names: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Turn selected folder labels into normalized scan roots."""

    selected = {_normalize_folder_name(name) for name in selected_names}
    roots: list[str] = []
    for folder in folders:
        if _normalize_folder_name(folder.name) in selected:
            roots.append(folder.url)
    missing = selected - {_normalize_folder_name(folder.name) for folder in folders}
    for name in sorted(missing):
        roots.append(urljoin(base_url.rstrip("/") + "/", name))
    return tuple(roots)


def _normalize_folder_name(name: str) -> str:
    stripped = name.strip().lstrip("/")
    return stripped if stripped.endswith("/") else f"{stripped}/"
