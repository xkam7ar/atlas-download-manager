"""Guided bootstrap/setup planning for Atlas."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atlas.config import AtlasSettings, settings_as_toml
from atlas.paths import config_path, ensure_app_dirs

ATLAS_REPOSITORY = "https://github.com/xkam7ar/atlas.git"
ATLAS_RAW_INSTALL_URL = (
    "https://raw.githubusercontent.com/xkam7ar/atlas/main/install.sh"
)


class SetupMode(StrEnum):
    """Supported setup footprints."""

    full = "full"
    minimal = "minimal"
    media_only = "media-only"
    mirrors = "mirrors"


@dataclass(frozen=True)
class RuntimeTool:
    """A runtime executable and its package-manager mapping."""

    executable: str
    package: str
    purpose: str
    modes: frozenset[SetupMode]
    required: bool = False


@dataclass(frozen=True)
class SetupEnvironment:
    """Detected host setup environment."""

    os_name: str
    architecture: str
    shell: str | None
    package_manager: str | None
    package_manager_path: str | None
    install_method: str
    atlas_executable: str | None


@dataclass(frozen=True)
class SetupPlan:
    """Concrete setup plan shown by `atlas setup` and `doctor --fix`."""

    mode: SetupMode
    environment: SetupEnvironment
    tools: tuple[RuntimeTool, ...]
    missing_tools: tuple[RuntimeTool, ...]
    existing_tools: tuple[RuntimeTool, ...]
    install_commands: tuple[tuple[str, ...], ...]
    manual_commands: tuple[str, ...]
    config_file: Path
    output_dir: Path
    can_install: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Return True when all selected runtime tools are present."""

        return not self.missing_tools


@dataclass(frozen=True)
class SetupResult:
    """Result of applying a setup plan."""

    commands_run: tuple[tuple[str, ...], ...]
    created_paths: tuple[Path, ...]
    config_written: bool


@dataclass(frozen=True)
class UpdatePlan:
    """Install-method-aware Atlas update plan."""

    install_method: str
    command: tuple[str, ...] | None
    detail: str
    can_update: bool


TOOL_SPECS: tuple[RuntimeTool, ...] = (
    RuntimeTool(
        executable="ffmpeg",
        package="ffmpeg",
        purpose="media download post-processing",
        modes=frozenset({SetupMode.full, SetupMode.minimal, SetupMode.media_only}),
        required=True,
    ),
    RuntimeTool(
        executable="ffprobe",
        package="ffmpeg",
        purpose="media metadata probing",
        modes=frozenset({SetupMode.full, SetupMode.minimal, SetupMode.media_only}),
        required=True,
    ),
    RuntimeTool(
        executable="aria2c",
        package="aria2",
        purpose="segmented direct-file downloads, Metalink, and shared batch queues",
        modes=frozenset({SetupMode.full}),
    ),
    RuntimeTool(
        executable="wget2",
        package="wget2",
        purpose="website and open-directory mirroring",
        modes=frozenset({SetupMode.full, SetupMode.mirrors}),
    ),
    RuntimeTool(
        executable="wget",
        package="wget",
        purpose="mirror fallback backend",
        modes=frozenset({SetupMode.full, SetupMode.mirrors}),
    ),
)


def selected_tools(mode: SetupMode) -> tuple[RuntimeTool, ...]:
    """Return runtime tools selected by a setup mode."""

    return tuple(tool for tool in TOOL_SPECS if mode in tool.modes)


def detect_setup_environment(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> SetupEnvironment:
    """Detect OS, shell, package manager, and likely Atlas install method."""

    resolved_env = os.environ if env is None else env
    brew_path = which("brew")
    uv_path = which("uv")
    package_manager = "homebrew" if brew_path else None
    package_manager_path = brew_path
    atlas_executable = which("atlas")
    return SetupEnvironment(
        os_name=_os_label(),
        architecture=platform.machine() or "unknown",
        shell=_shell_name(resolved_env.get("SHELL")),
        package_manager=package_manager,
        package_manager_path=package_manager_path,
        install_method=detect_install_method(which=which, uv_path=uv_path),
        atlas_executable=atlas_executable,
    )


def detect_install_method(
    *,
    which: Callable[[str], str | None] = shutil.which,
    uv_path: str | None = None,
) -> str:
    """Best-effort install-method detection for update guidance."""

    brew = which("brew")
    atlas_executable = which("atlas")
    if brew and _brew_has_formula("atlas", brew=brew):
        return "homebrew"
    if atlas_executable and "/uv/tools/" in atlas_executable:
        return "uv-tool"
    if uv_path and _running_from_uv_tool():
        return "uv-tool"
    if _source_checkout_root() is not None:
        return "source-checkout"
    return "unknown"


def build_setup_plan(
    settings: AtlasSettings,
    *,
    mode: SetupMode = SetupMode.full,
    env: SetupEnvironment | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> SetupPlan:
    """Build a setup plan without mutating the host."""

    environment = env or detect_setup_environment(which=which)
    tools = selected_tools(mode)
    existing_tools = tuple(tool for tool in tools if which(tool.executable) is not None)
    missing_tools = tuple(tool for tool in tools if which(tool.executable) is None)
    packages = _dedupe(tool.package for tool in missing_tools)
    install_commands: list[tuple[str, ...]] = []
    manual_commands: list[str] = []
    notes: list[str] = []
    if packages and environment.package_manager == "homebrew":
        brew = environment.package_manager_path or "brew"
        install_commands.append((brew, "install", *packages))
        manual_commands.append("brew install " + " ".join(packages))
    elif packages:
        notes.append(
            "No supported package manager was detected. Install the missing tools manually."
        )
        if environment.os_name == "macOS":
            manual_commands.append(
                '/bin/bash -c "$(curl -fsSL '
                'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
        manual_commands.extend(_manual_tool_commands(packages, os_name=environment.os_name))
    if environment.package_manager is None:
        notes.append("Homebrew was not detected; Atlas will not install it silently.")
    return SetupPlan(
        mode=mode,
        environment=environment,
        tools=tools,
        missing_tools=missing_tools,
        existing_tools=existing_tools,
        install_commands=tuple(install_commands),
        manual_commands=tuple(manual_commands),
        config_file=config_path(),
        output_dir=settings.output_dir,
        can_install=bool(install_commands),
        notes=tuple(notes),
    )


def apply_setup_plan(
    plan: SetupPlan,
    settings: AtlasSettings,
    *,
    install: bool,
    runner: Callable[[Sequence[str]], None] | None = None,
) -> SetupResult:
    """Create Atlas paths and optionally run install commands."""

    ensure_app_dirs()
    created_paths = [plan.config_file.parent, plan.output_dir]
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    config_written = False
    if not plan.config_file.exists():
        plan.config_file.parent.mkdir(parents=True, exist_ok=True)
        plan.config_file.write_text(
            settings_as_toml(settings, redact_sensitive=False),
            encoding="utf-8",
        )
        plan.config_file.chmod(0o600)
        config_written = True
    commands_run: list[tuple[str, ...]] = []
    if install:
        active_runner = runner or _run_command
        for command in plan.install_commands:
            active_runner(command)
            commands_run.append(command)
    return SetupResult(
        commands_run=tuple(commands_run),
        created_paths=tuple(created_paths),
        config_written=config_written,
    )


def build_update_plan(
    *,
    install_method: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> UpdatePlan:
    """Return the command Atlas should use for the detected install method."""

    method = install_method or detect_install_method(which=which)
    if method == "homebrew":
        return UpdatePlan(
            install_method=method,
            command=("brew", "upgrade", "xkam7ar/tap/atlas"),
            detail="Atlas appears to be installed through Homebrew.",
            can_update=True,
        )
    if method == "uv-tool":
        return UpdatePlan(
            install_method=method,
            command=("uv", "tool", "install", "--force", f"git+{ATLAS_REPOSITORY}"),
            detail="Atlas appears to be installed as a uv tool.",
            can_update=True,
        )
    if method == "source-checkout":
        root = _source_checkout_root()
        command = (
            ("git", "-C", str(root), "pull", "--ff-only")
            if root is not None
            else ("git", "pull", "--ff-only")
        )
        return UpdatePlan(
            install_method=method,
            command=command,
            detail=(
                f"Atlas appears to be running from a source checkout at {root}."
                if root is not None
                else "Atlas appears to be running from a source checkout."
            ),
            can_update=True,
        )
    return UpdatePlan(
        install_method=method,
        command=None,
        detail=(
            "Atlas could not determine its install method. Use Homebrew, the curl installer, "
            "or uv tool install to update."
        ),
        can_update=False,
    )


def run_update_plan(
    plan: UpdatePlan,
    *,
    runner: Callable[[Sequence[str]], None] | None = None,
) -> None:
    """Run the update command for an update plan."""

    if plan.command is None:
        msg = "Atlas could not determine an update command for this install method."
        raise RuntimeError(msg)
    (runner or _run_command)(plan.command)


def _os_label() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    return system or "unknown"


def _shell_name(shell: str | None) -> str | None:
    if not shell:
        return None
    return Path(shell).name


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _manual_tool_commands(packages: Sequence[str], *, os_name: str) -> list[str]:
    commands: list[str] = []
    if os_name == "Linux":
        commands.append("sudo apt install " + " ".join(packages))
    commands.append("brew install " + " ".join(packages))
    return commands


def _run_command(command: Sequence[str]) -> None:
    subprocess.run(list(command), check=True)


def _brew_has_formula(name: str, *, brew: str) -> bool:
    try:
        result = subprocess.run(
            [brew, "list", "--formula", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _running_from_uv_tool() -> bool:
    return "/uv/tools/" in sys.prefix or "/uv/tools/" in sys.executable


def _source_checkout_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None
