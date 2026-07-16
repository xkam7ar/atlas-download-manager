from __future__ import annotations

from pathlib import Path

import pytest

from atlas.paths import app_dirs, archive_path, config_path, default_output_dir, safe_filename


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


@pytest.mark.parametrize("stem", ["CON", "aux.txt", "COM1.log", "lpt9"])
def test_safe_filename_avoids_windows_reserved_device_names(stem: str) -> None:
    value = safe_filename(stem)

    assert value.partition(".")[0].upper() not in {"CON", "AUX", "COM1", "LPT9"}


@pytest.mark.parametrize("character", ["😀", "é"])
def test_safe_filename_respects_utf8_component_byte_budget(character: str) -> None:
    value = safe_filename(character * 200 + ".txt")

    assert len(value.encode("utf-8")) <= 240
    assert value.endswith(".txt")
