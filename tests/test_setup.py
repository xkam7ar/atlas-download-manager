from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.setup import (
    SetupEnvironment,
    SetupMode,
    apply_setup_plan,
    build_setup_plan,
    build_update_plan,
    selected_tools,
)


def test_full_homebrew_plan_installs_all_missing_runtime_packages(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    env = SetupEnvironment(
        os_name="macOS",
        architecture="arm64",
        shell="zsh",
        package_manager="homebrew",
        package_manager_path="/opt/homebrew/bin/brew",
        install_method="homebrew",
        atlas_executable="/opt/homebrew/bin/atlas",
    )

    plan = build_setup_plan(settings, env=env, which=lambda _name: None)

    assert plan.mode == SetupMode.full
    assert [tool.executable for tool in plan.tools] == [
        "ffmpeg",
        "ffprobe",
        "aria2c",
        "wget2",
        "wget",
    ]
    assert plan.install_commands == (
        ("/opt/homebrew/bin/brew", "install", "ffmpeg", "aria2", "wget2", "wget"),
    )
    assert plan.can_install is True


def test_minimal_setup_only_selects_media_essentials() -> None:
    assert [tool.executable for tool in selected_tools(SetupMode.minimal)] == [
        "ffmpeg",
        "ffprobe",
    ]


def test_mirror_setup_only_selects_mirror_backends() -> None:
    assert [tool.executable for tool in selected_tools(SetupMode.mirrors)] == ["wget2", "wget"]


def test_setup_plan_dedupes_ffmpeg_package(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    env = SetupEnvironment(
        os_name="macOS",
        architecture="arm64",
        shell="zsh",
        package_manager="homebrew",
        package_manager_path="/opt/homebrew/bin/brew",
        install_method="unknown",
        atlas_executable=None,
    )

    plan = build_setup_plan(settings, mode=SetupMode.media_only, env=env, which=lambda _name: None)

    assert plan.install_commands == (("/opt/homebrew/bin/brew", "install", "ffmpeg"),)


def test_apply_setup_plan_creates_output_and_config_without_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_file = tmp_path / "config" / "config.toml"
    monkeypatch.setattr("atlas.setup.config_path", lambda: config_file)
    settings = AtlasSettings(
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    env = SetupEnvironment(
        os_name="macOS",
        architecture="arm64",
        shell="zsh",
        package_manager=None,
        package_manager_path=None,
        install_method="unknown",
        atlas_executable=None,
    )
    plan = build_setup_plan(settings, env=env, which=lambda _name: None)
    commands: list[tuple[str, ...]] = []

    result = apply_setup_plan(
        plan,
        settings,
        install=False,
        runner=lambda command: commands.append(tuple(command)),
    )

    assert settings.output_dir.exists()
    assert config_file.exists()
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert "default_output_dir" in config_file.read_text(encoding="utf-8")
    assert result.commands_run == ()
    assert commands == []


def test_apply_setup_plan_runs_install_commands_when_requested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("atlas.setup.config_path", lambda: tmp_path / "config.toml")
    settings = AtlasSettings(
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    env = SetupEnvironment(
        os_name="macOS",
        architecture="arm64",
        shell="zsh",
        package_manager="homebrew",
        package_manager_path="/opt/homebrew/bin/brew",
        install_method="homebrew",
        atlas_executable="/opt/homebrew/bin/atlas",
    )
    plan = build_setup_plan(settings, mode=SetupMode.media_only, env=env, which=lambda _name: None)
    commands: list[tuple[str, ...]] = []

    result = apply_setup_plan(
        plan,
        settings,
        install=True,
        runner=lambda command: commands.append(tuple(command)),
    )

    assert result.commands_run == (("/opt/homebrew/bin/brew", "install", "ffmpeg"),)
    assert commands == [("/opt/homebrew/bin/brew", "install", "ffmpeg")]


def test_no_homebrew_plan_is_manual_and_non_installing(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    env = SetupEnvironment(
        os_name="macOS",
        architecture="arm64",
        shell="zsh",
        package_manager=None,
        package_manager_path=None,
        install_method="unknown",
        atlas_executable=None,
    )

    plan = build_setup_plan(settings, env=env, which=lambda _name: None)

    assert plan.can_install is False
    assert plan.install_commands == ()
    assert any("Homebrew/install/HEAD/install.sh" in command for command in plan.manual_commands)
    assert "brew install ffmpeg aria2 wget2 wget" in plan.manual_commands
    assert "Homebrew was not detected" in " ".join(plan.notes)


def test_linux_manual_plan_does_not_include_homebrew_installer(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    env = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=None,
        package_manager_path=None,
        install_method="unknown",
        atlas_executable=None,
    )

    plan = build_setup_plan(settings, env=env, which=lambda _name: None)

    assert any(command.startswith("sudo apt install") for command in plan.manual_commands)
    assert not any("Homebrew/install/HEAD" in command for command in plan.manual_commands)


def test_install_script_no_install_does_not_require_homebrew(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "install.sh"
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }

    result = subprocess.run(
        ["sh", str(script), "--no-install", "--no-menu", "--yes"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "Homebrew was not found" in output
    assert "atlas is not on PATH yet" in output


def test_update_plan_uses_install_method_specific_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.setup._source_checkout_root", lambda: tmp_path)

    assert build_update_plan(install_method="homebrew").command == (
        "brew",
        "upgrade",
        "xkam7ar/tap/atlas",
    )
    assert build_update_plan(install_method="uv-tool").command == (
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/xkam7ar/atlas.git",
    )
    assert build_update_plan(install_method="source-checkout").command == (
        "git",
        "-C",
        str(tmp_path),
        "pull",
        "--ff-only",
    )
    assert build_update_plan(install_method="unknown").can_update is False
