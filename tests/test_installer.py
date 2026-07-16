from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _installer_env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ATLAS_OS": "Linux",
        "ATLAS_TEST_BIN": str(bin_dir),
        "ATLAS_TEST_LOG": str(tmp_path / "commands.log"),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        ("apt-get", "apt-get install -y ffmpeg aria2 wget2 wget"),
        ("dnf", "dnf install -y ffmpeg-free aria2 wget2 wget1-wget"),
        (
            "pacman",
            "pacman -S --needed --noconfirm ffmpeg aria2 wget base-devel procps-ng curl file git",
        ),
    ],
)
def test_installer_plan_only_detects_linux_managers_without_mutation(
    tmp_path: Path,
    manager: str,
    expected: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / manager, "#!/bin/sh\nexit 0\n")
    _executable(bin_dir / "sudo", "#!/bin/sh\nexit 0\n")
    _executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    env = _installer_env(tmp_path, bin_dir)

    result = subprocess.run(
        ["/bin/sh", str(INSTALLER), "--full", "--no-install", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert expected in result.stdout
    assert "Plan only; no changes made." in result.stdout
    assert not (tmp_path / "commands.log").exists()
    assert not (tmp_path / "home").exists()
    if manager == "pacman":
        assert "/home/linuxbrew/.linuxbrew/bin/brew install wget2" in result.stdout
        assert "Homebrew/install/HEAD/install.sh" in result.stdout


def test_installer_plan_only_includes_macos_homebrew_bootstrap(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "id", "#!/bin/sh\necho 501\n")
    _executable(bin_dir / "uname", "#!/bin/sh\necho arm64\n")
    env = {
        **_installer_env(tmp_path, bin_dir),
        "ATLAS_OS": "Darwin",
        "PATH": str(bin_dir),
    }

    result = subprocess.run(
        ["/bin/sh", str(INSTALLER), "--full", "--no-install", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Homebrew/install/HEAD/install.sh" in result.stdout
    assert "/opt/homebrew/bin/brew install ffmpeg aria2 wget2 wget" in result.stdout
    assert "Plan only; no changes made." in result.stdout
    assert not (tmp_path / "commands.log").exists()


def test_installer_plan_only_does_not_execute_discovered_atlas(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    _executable(
        bin_dir / "atlas",
        '#!/bin/sh\necho "atlas $*" >> "$ATLAS_TEST_LOG"\nexit 0\n',
    )
    env = _installer_env(tmp_path, bin_dir)

    result = subprocess.run(
        ["/bin/sh", str(INSTALLER), "--minimal", "--no-install", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Atlas already installed" in result.stdout
    assert "Plan only; no changes made." in result.stdout
    assert not (tmp_path / "commands.log").exists()


def test_installer_bootstraps_uv_with_official_installer(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv_source = tmp_path / "fake-uv"
    fake_atlas_source = tmp_path / "fake-atlas"
    _executable(
        fake_uv_source,
        "#!/bin/sh\n"
        'echo "uv $*" >> "$ATLAS_TEST_LOG"\n'
        'if [ "${1:-}" = "tool" ]; then\n'
        '  /bin/cp "$ATLAS_FAKE_ATLAS_SOURCE" "$ATLAS_TEST_BIN/atlas"\n'
        '  /bin/chmod +x "$ATLAS_TEST_BIN/atlas"\n'
        "fi\n",
    )
    _executable(
        fake_atlas_source,
        '#!/bin/sh\necho "atlas $*" >> "$ATLAS_TEST_LOG"\nexit 0\n',
    )
    _executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    for tool in ("ffmpeg", "ffprobe", "aria2c", "wget2", "wget"):
        _executable(bin_dir / tool, "#!/bin/sh\nexit 0\n")
    _executable(
        bin_dir / "curl",
        "#!/bin/sh\n"
        'echo "curl $*" >> "$ATLAS_TEST_LOG"\n'
        "printf '%s\\n' '#!/bin/sh' "
        '\'/bin/cp "$ATLAS_FAKE_UV_SOURCE" "$ATLAS_TEST_BIN/uv"\' '
        "'/bin/chmod +x \"$ATLAS_TEST_BIN/uv\"'\n",
    )
    env = {
        **_installer_env(tmp_path, bin_dir),
        "ATLAS_FAKE_UV_SOURCE": str(fake_uv_source),
        "ATLAS_FAKE_ATLAS_SOURCE": str(fake_atlas_source),
    }

    result = subprocess.run(
        ["sh", str(INSTALLER), "--full", "--yes", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    log = (tmp_path / "commands.log").read_text(encoding="utf-8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "curl -LsSf https://astral.sh/uv/install.sh" in log
    assert "uv tool install --force git+" in log
    assert "atlas setup --full --no-install" in log


def test_installer_yes_runs_plan_once_and_second_run_is_idempotent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    _executable(
        bin_dir / "sudo",
        '#!/bin/sh\necho "sudo $*" >> "$ATLAS_TEST_LOG"\nexec "$@"\n',
    )
    _executable(
        bin_dir / "apt-get",
        "#!/bin/sh\n"
        'echo "apt-get $*" >> "$ATLAS_TEST_LOG"\n'
        'if [ "${1:-}" = "install" ]; then\n'
        "  for tool in ffmpeg ffprobe aria2c wget2 wget; do\n"
        "    printf '#!/bin/sh\\nexit 0\\n' > \"$ATLAS_TEST_BIN/$tool\"\n"
        '    chmod +x "$ATLAS_TEST_BIN/$tool"\n'
        "  done\n"
        "fi\n",
    )
    _executable(
        bin_dir / "atlas",
        '#!/bin/sh\necho "atlas $*" >> "$ATLAS_TEST_LOG"\nexit 0\n',
    )
    env = _installer_env(tmp_path, bin_dir)

    first = subprocess.run(
        ["sh", str(INSTALLER), "--full", "--yes", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    first_log = log.read_text(encoding="utf-8")
    second = subprocess.run(
        ["sh", str(INSTALLER), "--full", "--yes", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    second_log = log.read_text(encoding="utf-8")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "[Y/n]" not in first.stdout + first.stderr
    assert sum(line.startswith("apt-get ") for line in first_log.splitlines()) == 2
    assert sum(line.startswith("apt-get ") for line in second_log.splitlines()) == 2
    assert "atlas setup --full\natlas setup --full --no-install" in second_log
    assert "atlas doctor" in second_log
    assert (
        INSTALLER.read_text(encoding="utf-8").count(
            'confirm_plan "Install Atlas and all listed prerequisites?"'
        )
        == 1
    )


def test_installer_stops_when_package_install_fails(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    _executable(bin_dir / "sudo", '#!/bin/sh\nexec "$@"\n')
    _executable(
        bin_dir / "apt-get",
        '#!/bin/sh\necho "apt-get $*" >> "$ATLAS_TEST_LOG"\nexit 23\n',
    )
    _executable(
        bin_dir / "atlas",
        '#!/bin/sh\necho "atlas $*" >> "$ATLAS_TEST_LOG"\nexit 0\n',
    )
    env = _installer_env(tmp_path, bin_dir)

    result = subprocess.run(
        ["sh", str(INSTALLER), "--full", "--yes", "--no-menu"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 23
    assert "apt-get update" in log.read_text(encoding="utf-8")
    assert "atlas setup --full" not in log.read_text(encoding="utf-8")
