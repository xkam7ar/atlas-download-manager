from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from atlas.cli import app
from atlas.passthrough import (
    BackendCommandPlan,
    BackendTool,
    plan_backend_command,
    run_backend_command,
)
from atlas.runner import SubprocessResult
from atlas.setup import PackageManager, SetupEnvironment

runner = CliRunner()


def test_ytdlp_dry_run_preserves_backend_args() -> None:
    result = runner.invoke(
        app,
        [
            "ytdlp",
            "--dry-run",
            "--json",
            "--",
            "--format",
            "bv*+ba/b",
            "https://example.com/watch?v=abc",
        ],
    )

    assert result.exit_code == 0
    assert '"tool": "yt-dlp"' in result.output
    assert '"--format"' in result.output
    assert '"bv*+ba/b"' in result.output
    assert "https://example.com/watch?v=abc" in result.output
    assert '"shell"' not in result.output


def test_ytdlp_dry_run_redacts_sensitive_backend_args() -> None:
    result = runner.invoke(
        app,
        [
            "ytdlp",
            "--dry-run",
            "--json",
            "--",
            "--username",
            "audit-user",
            "--password",
            "ATLAS_LEAK_SENTINEL",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "ATLAS_LEAK_SENTINEL" not in result.output
    assert "audit-user" not in result.output
    assert payload["args"][-1] == "<redacted>"


def test_aria2_dry_run_uses_resolved_binary(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(app, ["aria2", "--dry-run", "--json", "--", "--version"])

    assert result.exit_code == 0
    assert '"tool": "aria2c"' in result.output
    assert '"/opt/bin/aria2c"' in result.output
    assert '"--version"' in result.output


def test_missing_backend_reports_install_hint(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "atlas.setup.detect_setup_environment",
        lambda: SetupEnvironment(
            os_name="macOS",
            architecture="arm64",
            shell="zsh",
            package_manager=PackageManager.homebrew,
            package_manager_path="/opt/homebrew/bin/brew",
            install_method="unknown",
            atlas_executable=None,
        ),
    )

    result = runner.invoke(app, ["wget2", "--dry-run", "--", "--version"])

    assert result.exit_code == 1
    assert "wget2 is not installed" in result.output
    assert "brew install wget2" in result.output
    assert "Traceback" not in result.output


def test_backend_json_execution_reports_result(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda name: f"/opt/bin/{name}")

    def fake_run(plan, *, timeout, stream, on_line=None):
        assert plan.tool == BackendTool.wget
        assert timeout is None
        assert stream is False
        assert on_line is None
        return SubprocessResult(
            args=plan.command,
            returncode=0,
            stdout="GNU Wget 1.24",
            stderr="",
        )

    monkeypatch.setattr("atlas.cli.run_backend_command", fake_run)

    result = runner.invoke(app, ["wget", "--json", "--", "--version"])

    assert result.exit_code == 0
    assert '"returncode": 0' in result.output
    assert "GNU Wget 1.24" in result.output


def test_backend_json_execution_redacts_captured_output(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda name: f"/opt/bin/{name}")

    def fake_run(plan, *, timeout, stream, on_line=None):
        return SubprocessResult(
            args=plan.command,
            returncode=0,
            stdout="download https://example.test/file?token=ATLAS_LEAK_SENTINEL",
            stderr="Authorization: ATLAS_LEAK_SENTINEL",
        )

    monkeypatch.setattr("atlas.cli.run_backend_command", fake_run)

    result = runner.invoke(app, ["wget", "--json", "--", "--version"])

    assert result.exit_code == 0
    assert "ATLAS_LEAK_SENTINEL" not in result.output
    payload = json.loads(result.output)
    assert "<redacted>" in payload["stdout"]
    assert payload["stderr"] == "<redacted>"


def test_backend_timeout_does_not_expose_raw_command(monkeypatch) -> None:
    def fake_run(plan, *, timeout, stream, on_line=None):
        raise subprocess.TimeoutExpired(plan.command, timeout or 0)

    monkeypatch.setattr("atlas.cli.run_backend_command", fake_run)

    result = runner.invoke(
        app,
        ["ytdlp", "--timeout", "0", "--", "--password", "ATLAS_LEAK_SENTINEL"],
    )

    assert result.exit_code == 1
    assert "exceeded its 0-second timeout" in result.output
    assert "ATLAS_LEAK_SENTINEL" not in result.output
    assert "Traceback" not in result.output


def test_backend_plan_for_ytdlp_uses_python_module() -> None:
    plan = plan_backend_command(BackendTool.ytdlp, ["--version"], cwd=Path("/tmp"))

    assert plan.command[-3:] == ["-m", "yt_dlp", "--version"]
    assert plan.cwd == Path("/tmp")
    assert "shell is never used" in " ".join(plan.safety)


def test_backend_execution_honors_planned_working_directory(tmp_path: Path) -> None:
    plan = BackendCommandPlan(
        tool=BackendTool.ytdlp,
        display_name="python",
        command=[sys.executable, "-c", "import os; print(os.getcwd())"],
        user_args=[],
        cwd=tmp_path,
        safety=(),
    )

    result = run_backend_command(plan, timeout=5, stream=False)

    assert result.returncode == 0
    assert Path(result.stdout.strip()) == tmp_path
