from __future__ import annotations

from pathlib import Path

from atlas.paths import app_dirs, archive_path, config_path, default_output_dir


def test_platformdirs_paths() -> None:
    cfg_path = config_path()
    data_path = archive_path()
    directories = app_dirs()

    assert cfg_path.name == "config.toml"
    assert cfg_path.parent.name == "atlas"
    assert cfg_path.parent == Path(directories.user_config_dir)
    assert data_path.name == "download-archive.txt"
    assert data_path.parent.name == "atlas"
    assert data_path.parent == Path(directories.user_data_dir)


def test_default_output_dir() -> None:
    assert default_output_dir() == Path.home() / "Downloads" / "atlas"
