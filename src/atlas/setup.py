"""Guided bootstrap/setup planning for Atlas."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atlas.config import AtlasSettings, settings_as_toml
from atlas.paths import config_path, ensure_app_dirs
from atlas.private_files import write_private_text

ATLAS_REPOSITORY = "https://github.com/xkam7ar/atlas.git"
HOMEBREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
LINUXBREW_PATH = "/home/linuxbrew/.linuxbrew/bin/brew"
RELEASE_REF_PLACEHOLDER = "<40-character-commit-id>"


class SetupMode(StrEnum):
    """Supported setup footprints."""

    full = "full"
    minimal = "minimal"
    media_only = "media-only"
    mirrors = "mirrors"


class PackageManager(StrEnum):
    """System package managers Atlas can drive automatically."""

    homebrew = "homebrew"
    apt = "apt"
    dnf = "dnf"
    pacman = "pacman"


@dataclass(frozen=True)
class RuntimeTool:
    """A runtime executable and its package-manager mapping."""

    executable: str
    packages: Mapping[PackageManager, str | None]
    purpose: str
    modes: frozenset[SetupMode]
    required: bool = False

    @property
    def package(self) -> str:
        """Return the canonical Homebrew package name for compatibility."""

        return self.packages[PackageManager.homebrew] or self.executable

    def package_for(self, manager: PackageManager | str | None) -> str | None:
        """Return the package providing this executable for a package manager."""

        resolved = _package_manager(manager)
        return self.packages.get(resolved) if resolved is not None else None


@dataclass(frozen=True)
class SetupEnvironment:
    """Detected host setup environment."""

    os_name: str
    architecture: str
    shell: str | None
    package_manager: PackageManager | str | None
    package_manager_path: str | None
    install_method: str
    atlas_executable: str | None
    is_root: bool = False
    elevation_tool: str | None = None


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
        packages={
            PackageManager.homebrew: "ffmpeg",
            PackageManager.apt: "ffmpeg",
            PackageManager.dnf: "ffmpeg-free",
            PackageManager.pacman: "ffmpeg",
        },
        purpose="media download post-processing",
        modes=frozenset({SetupMode.full, SetupMode.minimal, SetupMode.media_only}),
        required=True,
    ),
    RuntimeTool(
        executable="ffprobe",
        packages={
            PackageManager.homebrew: "ffmpeg",
            PackageManager.apt: "ffmpeg",
            PackageManager.dnf: "ffmpeg-free",
            PackageManager.pacman: "ffmpeg",
        },
        purpose="media metadata probing",
        modes=frozenset({SetupMode.full, SetupMode.minimal, SetupMode.media_only}),
        required=True,
    ),
    RuntimeTool(
        executable="aria2c",
        packages={
            PackageManager.homebrew: "aria2",
            PackageManager.apt: "aria2",
            PackageManager.dnf: "aria2",
            PackageManager.pacman: "aria2",
        },
        purpose="segmented direct-file downloads, Metalink, and shared batch queues",
        modes=frozenset({SetupMode.full}),
    ),
    RuntimeTool(
        executable="wget2",
        packages={
            PackageManager.homebrew: "wget2",
            PackageManager.apt: "wget2",
            PackageManager.dnf: "wget2",
            PackageManager.pacman: None,
        },
        purpose="website and open-directory mirroring",
        modes=frozenset({SetupMode.full, SetupMode.mirrors}),
    ),
    RuntimeTool(
        executable="wget",
        packages={
            PackageManager.homebrew: "wget",
            PackageManager.apt: "wget",
            PackageManager.dnf: "wget1-wget",
            PackageManager.pacman: "wget",
        },
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
    is_root: bool | None = None,
) -> SetupEnvironment:
    """Detect OS, shell, package manager, and likely Atlas install method."""

    resolved_env = os.environ if env is None else env
    os_name = _os_label()
    package_manager, package_manager_path = _detect_package_manager(
        os_name=os_name,
        which=which,
    )
    uv_path = which("uv")
    atlas_executable = which("atlas")
    resolved_is_root = _is_root() if is_root is None else is_root
    return SetupEnvironment(
        os_name=os_name,
        architecture=platform.machine() or "unknown",
        shell=_shell_name(resolved_env.get("SHELL")),
        package_manager=package_manager,
        package_manager_path=package_manager_path,
        install_method=detect_install_method(which=which, uv_path=uv_path),
        atlas_executable=atlas_executable,
        is_root=resolved_is_root,
        elevation_tool=None if resolved_is_root else which("sudo"),
    )


def detect_install_method(
    *,
    which: Callable[[str], str | None] = shutil.which,
    uv_path: str | None = None,
) -> str:
    """Best-effort install-method detection for update guidance."""

    atlas_executable = which("atlas")
    resolved_executable = (
        Path(atlas_executable).expanduser().resolve(strict=False) if atlas_executable else None
    )
    if resolved_executable is not None and "/uv/tools/" in resolved_executable.as_posix():
        return "uv-tool"
    if _running_from_uv_tool():
        return "uv-tool"
    if resolved_executable is not None and _is_atlas_homebrew_install(resolved_executable):
        return "homebrew"
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
    install_commands: list[tuple[str, ...]] = []
    manual_commands: list[str] = []
    notes: list[str] = []
    manager = _package_manager(environment.package_manager)
    if missing_tools:
        install_commands, manual_commands, notes = _build_install_commands(
            missing_tools,
            environment=environment,
            manager=manager,
            which=which,
        )
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


def package_for_environment(tool: RuntimeTool, environment: SetupEnvironment) -> str:
    """Return the package shown for a tool on the detected host."""

    manager = _package_manager(environment.package_manager)
    package = tool.package_for(manager)
    if package is not None:
        return package
    if manager == PackageManager.pacman:
        return tool.package_for(PackageManager.homebrew) or tool.executable
    return tool.package


def install_hint_for_tool(
    executable: str,
    *,
    environment: SetupEnvironment | None = None,
) -> str:
    """Return a host-aware one-line install hint for a runtime tool."""

    if executable == "yt-dlp":
        return f"atlas update --release-ref {RELEASE_REF_PLACEHOLDER}"
    if executable == "atlas package":
        return "brew install xkam7ar/tap/atlas"
    tool = next((item for item in TOOL_SPECS if item.executable == executable), None)
    if tool is None:
        return "atlas doctor"
    resolved = environment or detect_setup_environment()
    manager = _package_manager(resolved.package_manager)
    package = package_for_environment(tool, resolved)
    if manager == PackageManager.homebrew or (
        manager == PackageManager.pacman and tool.package_for(manager) is None
    ):
        return f"brew install {package}"
    prefix = "" if resolved.is_root else "sudo "
    if manager == PackageManager.apt:
        return f"{prefix}apt-get install -y {package}"
    if manager == PackageManager.dnf:
        return f"{prefix}dnf install -y {package}"
    if manager == PackageManager.pacman:
        return f"{prefix}pacman -S --needed {package}"
    return f"brew install {tool.package}"


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
        write_private_text(
            plan.config_file,
            settings_as_toml(settings, redact_sensitive=False),
        )
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
    release_ref: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> UpdatePlan:
    """Return a safe update command for the detected install method.

    Remote uv-tool updates require an explicit full commit ID so
    making the repository public cannot silently turn the default branch into
    an update channel. Source-checkout updates remain a deliberate local
    development workflow.
    """

    method = install_method or detect_install_method(which=which)
    if method == "homebrew":
        return UpdatePlan(
            install_method=method,
            command=("brew", "upgrade", "xkam7ar/tap/atlas"),
            detail="Atlas appears to be installed through Homebrew.",
            can_update=True,
        )
    if method == "uv-tool":
        if not is_immutable_release_ref(release_ref):
            return UpdatePlan(
                install_method=method,
                command=None,
                detail=(
                    "Atlas remote updates are disabled for mutable branches. "
                    "Resolve the intended release tag and pass --release-ref with its full "
                    "40-character commit ID."
                ),
                can_update=False,
            )
        assert release_ref is not None
        resolved_ref = release_ref.strip()
        return UpdatePlan(
            install_method=method,
            command=(
                "uv",
                "tool",
                "install",
                "--force",
                f"git+{ATLAS_REPOSITORY}@{resolved_ref}",
            ),
            detail=f"Atlas appears to be installed as a uv tool; release ref: {resolved_ref}.",
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
            "Atlas could not determine its install method. Reinstall from this local checkout "
            "or from a verified, immutable release artifact."
        ),
        can_update=False,
    )


def is_immutable_release_ref(release_ref: str | None) -> bool:
    """Return whether a ref is a full Git commit object ID."""

    if release_ref is None:
        return False
    candidate = release_ref.strip()
    return len(candidate) == 40 and all(
        character in "0123456789abcdefABCDEF" for character in candidate
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


def _package_manager(value: PackageManager | str | None) -> PackageManager | None:
    if value is None:
        return None
    try:
        return PackageManager(value)
    except ValueError:
        return None


def _detect_package_manager(
    *,
    os_name: str,
    which: Callable[[str], str | None],
) -> tuple[PackageManager | None, str | None]:
    brew = which("brew")
    if os_name == "macOS":
        return (PackageManager.homebrew, brew) if brew else (None, None)
    if os_name == "Linux":
        for manager, executable in (
            (PackageManager.apt, "apt-get"),
            (PackageManager.dnf, "dnf"),
            (PackageManager.pacman, "pacman"),
        ):
            path = which(executable)
            if path:
                return manager, path
    return (PackageManager.homebrew, brew) if brew else (None, None)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def _elevation_prefix(environment: SetupEnvironment) -> tuple[str, ...] | None:
    if environment.is_root:
        return ()
    if environment.elevation_tool:
        return (environment.elevation_tool,)
    return None


def _homebrew_install_command() -> tuple[str, ...]:
    script = f'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL {HOMEBREW_INSTALL_URL})"'
    return ("/bin/bash", "-c", script)


def _homebrew_path(*, os_name: str, architecture: str) -> str:
    if os_name == "Linux":
        return LINUXBREW_PATH
    return "/opt/homebrew/bin/brew" if architecture == "arm64" else "/usr/local/bin/brew"


def _build_install_commands(
    missing_tools: Sequence[RuntimeTool],
    *,
    environment: SetupEnvironment,
    manager: PackageManager | None,
    which: Callable[[str], str | None],
) -> tuple[list[tuple[str, ...]], list[str], list[str]]:
    commands: list[tuple[str, ...]] = []
    manual: list[str] = []
    notes: list[str] = []
    if manager == PackageManager.homebrew:
        packages = _dedupe(
            package for tool in missing_tools if (package := tool.package_for(manager)) is not None
        )
        brew = environment.package_manager_path or "brew"
        command = (brew, "install", *packages)
        return [command], [_command_text(command)], notes

    if manager in {PackageManager.apt, PackageManager.dnf}:
        packages = _dedupe(
            package for tool in missing_tools if (package := tool.package_for(manager)) is not None
        )
        prefix = _elevation_prefix(environment)
        manual_prefix = () if environment.is_root else ("sudo",)
        if manager == PackageManager.apt:
            manager_path = environment.package_manager_path or "apt-get"
            native_commands = [
                (*manual_prefix, manager_path, "update"),
                (*manual_prefix, manager_path, "install", "-y", *packages),
            ]
            if prefix is not None:
                commands = [
                    (*prefix, manager_path, "update"),
                    (*prefix, manager_path, "install", "-y", *packages),
                ]
        else:
            manager_path = environment.package_manager_path or "dnf"
            native_commands = [
                (*manual_prefix, manager_path, "install", "-y", *packages),
            ]
            if prefix is not None:
                commands = [
                    (*prefix, manager_path, "install", "-y", *packages),
                ]
        manual = [_command_text(command) for command in native_commands]
        if prefix is None:
            notes.append("Root access or sudo is required to install missing system packages.")
        return commands, manual, notes

    if manager == PackageManager.pacman:
        native_packages = _dedupe(
            package for tool in missing_tools if (package := tool.package_for(manager)) is not None
        )
        brew_tools = tuple(tool for tool in missing_tools if tool.package_for(manager) is None)
        brew_path = which("brew")
        bootstrap_brew = bool(brew_tools and brew_path is None)
        if bootstrap_brew:
            native_packages = _dedupe(
                (*native_packages, "base-devel", "procps-ng", "curl", "file", "git")
            )
        prefix = _elevation_prefix(environment)
        manual_prefix = () if environment.is_root else ("sudo",)
        manager_path = environment.package_manager_path or "pacman"
        if native_packages:
            native = (
                *manual_prefix,
                manager_path,
                "-S",
                "--needed",
                "--noconfirm",
                *native_packages,
            )
            manual.append(_command_text(native))
            if prefix is not None:
                commands.append(
                    (
                        *prefix,
                        manager_path,
                        "-S",
                        "--needed",
                        "--noconfirm",
                        *native_packages,
                    )
                )
        if bootstrap_brew:
            brew_install = _homebrew_install_command()
            manual.append(_command_text(brew_install))
            if prefix is not None:
                commands.append(brew_install)
            brew_path = LINUXBREW_PATH
        if brew_tools:
            brew_packages = _dedupe(tool.package for tool in brew_tools)
            brew_command = (brew_path or LINUXBREW_PATH, "install", *brew_packages)
            manual.append(_command_text(brew_command))
            if not bootstrap_brew or prefix is not None:
                commands.append(brew_command)
            notes.append("wget2 will be installed through Linuxbrew on this pacman host.")
        if prefix is None and native_packages:
            commands = []
            notes.append("Root access or sudo is required to install missing system packages.")
        return commands, manual, notes

    homebrew_packages = _dedupe(tool.package for tool in missing_tools)
    if environment.os_name == "macOS":
        brew = _homebrew_path(
            os_name=environment.os_name,
            architecture=environment.architecture,
        )
        commands = [
            _homebrew_install_command(),
            (brew, "install", *homebrew_packages),
        ]
        manual = [_command_text(command) for command in commands]
        notes.append("Homebrew will be installed after approval.")
        return commands, manual, notes

    notes.append("No supported package manager was detected. Install the missing tools manually.")
    manual = _manual_tool_commands(homebrew_packages, os_name=environment.os_name)
    return commands, manual, notes


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _manual_tool_commands(packages: Sequence[str], *, os_name: str) -> list[str]:
    commands: list[str] = []
    if os_name == "Linux":
        commands.extend(
            (
                "sudo apt-get install -y " + " ".join(packages),
                "sudo dnf install -y " + " ".join(packages),
                "sudo pacman -S --needed " + " ".join(packages),
            )
        )
    commands.append("brew install " + " ".join(packages))
    return commands


def _command_text(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _run_command(command: Sequence[str]) -> None:
    subprocess.run(list(command), check=True)


def _is_atlas_homebrew_install(executable: Path) -> bool:
    """Identify this tap's formula without invoking Homebrew or matching core/atlas."""

    for parent in executable.parents:
        receipt = parent / "INSTALL_RECEIPT.json"
        if not receipt.is_file():
            continue
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        source = payload.get("source")
        return isinstance(source, dict) and source.get("tap") == "xkam7ar/tap"
    return False


def _running_from_uv_tool() -> bool:
    return "/uv/tools/" in sys.prefix or "/uv/tools/" in sys.executable


def _source_checkout_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None
