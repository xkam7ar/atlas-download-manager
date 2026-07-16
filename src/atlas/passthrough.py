"""Advanced raw backend command support."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from atlas.errors import EngineError
from atlas.runner import ProcessControl, SubprocessResult, run_args, run_args_stream
from atlas.setup import install_hint_for_tool


class BackendTool(StrEnum):
    ytdlp = "yt-dlp"
    aria2 = "aria2c"
    wget = "wget"
    wget2 = "wget2"


@dataclass(frozen=True)
class BackendCommandPlan:
    tool: BackendTool
    display_name: str
    command: list[str]
    user_args: list[str]
    cwd: Path
    safety: tuple[str, ...]


def plan_backend_command(
    tool: BackendTool,
    user_args: list[str],
    *,
    cwd: Path | None = None,
) -> BackendCommandPlan:
    """Build a safe argv command for an advanced backend pass-through."""

    resolved_cwd = cwd or Path.cwd()
    executable = _backend_executable(tool)
    command = [*executable, *user_args]
    return BackendCommandPlan(
        tool=tool,
        display_name=tool.value,
        command=command,
        user_args=user_args,
        cwd=resolved_cwd,
        safety=(
            "advanced backend mode",
            "argv array execution; shell is never used",
            "atlas does not reinterpret backend-specific flags",
        ),
    )


def plan_as_dict(plan: BackendCommandPlan) -> dict[str, Any]:
    return {
        "tool": plan.tool.value,
        "command": plan.command,
        "args": plan.user_args,
        "cwd": str(plan.cwd),
        "safety": list(plan.safety),
    }


def run_backend_command(
    plan: BackendCommandPlan,
    *,
    timeout: float | None,
    stream: bool,
    on_line: Any | None = None,
    control: ProcessControl | None = None,
) -> SubprocessResult:
    """Run a backend command without a shell."""

    if stream:
        callback = on_line if callable(on_line) else (lambda _line: None)
        return run_args_stream(
            plan.command,
            on_line=callback,
            timeout=timeout,
            control=control,
        )
    return run_args(plan.command, timeout=timeout, control=control)


def _backend_executable(tool: BackendTool) -> list[str]:
    if tool == BackendTool.ytdlp:
        return [sys.executable, "-m", "yt_dlp"]
    executable = shutil.which(tool.value)
    if executable is None:
        install = _install_hint(tool)
        raise EngineError(f"{tool.value} is not installed. Install it with `{install}`.")
    return [executable]


def _install_hint(tool: BackendTool) -> str:
    return install_hint_for_tool(tool.value)
