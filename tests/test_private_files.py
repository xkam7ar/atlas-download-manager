from __future__ import annotations

from pathlib import Path

import pytest

from atlas.private_files import (
    ensure_private_directory,
    replace_private_text,
    write_private_text,
)


def test_private_files_use_owner_only_permissions(tmp_path: Path) -> None:
    directory = ensure_private_directory(tmp_path / "private")
    path = directory / "urls.txt"

    write_private_text(path, "https://example.com/signed\n")

    assert directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_private_create_refuses_existing_files_and_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "urls.txt"
    link.symlink_to(target)

    with pytest.raises(OSError):
        write_private_text(link, "replace")

    assert target.read_text(encoding="utf-8") == "keep"


def test_private_replace_swaps_a_symlink_without_following_it(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "metadata.json"
    link.symlink_to(target)

    replace_private_text(link, "private")

    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "private"
    assert link.stat().st_mode & 0o777 == 0o600
    assert target.read_text(encoding="utf-8") == "keep"
