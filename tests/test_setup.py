from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.setup import (
    PackageManager,
    SetupEnvironment,
    SetupMode,
    apply_setup_plan,
    build_setup_plan,
    build_update_plan,
    detect_install_method,
    detect_setup_environment,
    install_hint_for_tool,
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


def test_apply_setup_plan_refuses_dangling_config_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "config" / "config.toml"
    config_file.parent.mkdir()
    victim = tmp_path / "victim.toml"
    config_file.symlink_to(victim)
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

    with pytest.raises(FileExistsError):
        apply_setup_plan(plan, settings, install=False)

    assert not victim.exists()


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


def test_missing_homebrew_plan_bootstraps_after_approval(tmp_path: Path) -> None:
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

    assert plan.can_install is True
    assert plan.install_commands[0][:2] == ("/bin/bash", "-c")
    assert plan.install_commands[1] == (
        "/opt/homebrew/bin/brew",
        "install",
        "ffmpeg",
        "aria2",
        "wget2",
        "wget",
    )
    assert any("Homebrew/install/HEAD/install.sh" in command for command in plan.manual_commands)
    assert any(
        command.endswith("brew install ffmpeg aria2 wget2 wget") for command in plan.manual_commands
    )
    assert "Homebrew will be installed" in " ".join(plan.notes)


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

    assert any(command.startswith("sudo apt-get install") for command in plan.manual_commands)
    assert not any("Homebrew/install/HEAD" in command for command in plan.manual_commands)


@pytest.mark.parametrize(
    ("executable", "manager"),
    [
        ("apt-get", PackageManager.apt),
        ("dnf", PackageManager.dnf),
        ("pacman", PackageManager.pacman),
    ],
)
def test_detect_setup_environment_prefers_native_linux_manager(
    executable: str,
    manager: PackageManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.setup._os_label", lambda: "Linux")
    paths = {
        executable: f"/usr/bin/{executable}",
        "brew": "/home/linuxbrew/.linuxbrew/bin/brew",
        "sudo": "/usr/bin/sudo",
    }

    environment = detect_setup_environment(
        which=lambda name: paths.get(name),
        is_root=False,
    )

    assert environment.package_manager == manager
    assert environment.package_manager_path == f"/usr/bin/{executable}"
    assert environment.elevation_tool == "/usr/bin/sudo"
    assert environment.is_root is False


def test_apt_plan_updates_metadata_and_installs_only_missing_packages(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    environment = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=PackageManager.apt,
        package_manager_path="/usr/bin/apt-get",
        install_method="uv-tool",
        atlas_executable="/home/user/.local/bin/atlas",
        elevation_tool="/usr/bin/sudo",
    )

    plan = build_setup_plan(
        settings,
        env=environment,
        which=lambda name: f"/usr/bin/{name}" if name in {"ffmpeg", "ffprobe"} else None,
    )

    assert plan.install_commands == (
        ("/usr/bin/sudo", "/usr/bin/apt-get", "update"),
        (
            "/usr/bin/sudo",
            "/usr/bin/apt-get",
            "install",
            "-y",
            "aria2",
            "wget2",
            "wget",
        ),
    )


def test_dnf_plan_uses_fedora_package_names_without_sudo_for_root(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    environment = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=PackageManager.dnf,
        package_manager_path="/usr/bin/dnf",
        install_method="uv-tool",
        atlas_executable="/root/.local/bin/atlas",
        is_root=True,
    )

    plan = build_setup_plan(settings, env=environment, which=lambda _name: None)

    assert plan.install_commands == (
        (
            "/usr/bin/dnf",
            "install",
            "-y",
            "ffmpeg-free",
            "aria2",
            "wget2",
            "wget1-wget",
        ),
    )


def test_pacman_plan_bootstraps_linuxbrew_for_wget2(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    environment = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=PackageManager.pacman,
        package_manager_path="/usr/bin/pacman",
        install_method="uv-tool",
        atlas_executable="/home/user/.local/bin/atlas",
        elevation_tool="/usr/bin/sudo",
    )

    plan = build_setup_plan(settings, env=environment, which=lambda _name: None)

    assert plan.install_commands[0] == (
        "/usr/bin/sudo",
        "/usr/bin/pacman",
        "-S",
        "--needed",
        "--noconfirm",
        "ffmpeg",
        "aria2",
        "wget",
        "base-devel",
        "procps-ng",
        "curl",
        "file",
        "git",
    )
    assert plan.install_commands[1][:2] == ("/bin/bash", "-c")
    assert plan.install_commands[2] == (
        "/home/linuxbrew/.linuxbrew/bin/brew",
        "install",
        "wget2",
    )
    assert "Linuxbrew" in " ".join(plan.notes)


def test_native_linux_plan_requires_root_or_sudo(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    environment = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=PackageManager.apt,
        package_manager_path="/usr/bin/apt-get",
        install_method="unknown",
        atlas_executable=None,
    )

    plan = build_setup_plan(settings, env=environment, which=lambda _name: None)

    assert plan.can_install is False
    assert plan.install_commands == ()
    assert any(command.startswith("sudo /usr/bin/apt-get") for command in plan.manual_commands)
    assert "Root access or sudo" in " ".join(plan.notes)


def test_install_hint_uses_detected_manager() -> None:
    environment = SetupEnvironment(
        os_name="Linux",
        architecture="x86_64",
        shell="bash",
        package_manager=PackageManager.dnf,
        package_manager_path="/usr/bin/dnf",
        install_method="unknown",
        atlas_executable=None,
        elevation_tool="/usr/bin/sudo",
    )

    assert install_hint_for_tool("ffmpeg", environment=environment) == (
        "sudo dnf install -y ffmpeg-free"
    )
    assert install_hint_for_tool("wget", environment=environment) == (
        "sudo dnf install -y wget1-wget"
    )


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
    assert "Package manager: none" in output
    assert "Homebrew/install/HEAD/install.sh" in output
    assert "Plan only; no changes made." in output
    assert list(tmp_path.iterdir()) == []


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


def test_detect_install_method_resolves_uv_tool_launcher_symlink(tmp_path: Path) -> None:
    tool_executable = tmp_path / ".local" / "share" / "uv" / "tools" / "atlas" / "bin" / "atlas"
    tool_executable.parent.mkdir(parents=True)
    tool_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / ".local" / "bin" / "atlas"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(tool_executable)

    assert (
        detect_install_method(
            which=lambda name: str(launcher) if name == "atlas" else None,
        )
        == "uv-tool"
    )


def test_detect_install_method_distinguishes_tap_receipt_from_core_formula(
    tmp_path: Path,
) -> None:
    version = tmp_path / "Cellar" / "atlas" / "0.1.0"
    executable = version / "libexec" / "bin" / "atlas"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    receipt = version / "INSTALL_RECEIPT.json"
    receipt.write_text('{"source":{"tap":"homebrew/core"}}', encoding="utf-8")

    def which(name: str) -> str | None:
        return str(executable) if name == "atlas" else None

    assert detect_install_method(which=which) != "homebrew"

    receipt.write_text('{"source":{"tap":"xkam7ar/tap"}}', encoding="utf-8")
    assert detect_install_method(which=which) == "homebrew"
