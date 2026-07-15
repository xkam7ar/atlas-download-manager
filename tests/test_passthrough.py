from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from atlas.cli import app
from atlas.passthrough import BackendTool, plan_backend_command
from atlas.runner import SubprocessResult

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


def test_aria2_dry_run_uses_resolved_binary(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(app, ["aria2", "--dry-run", "--json", "--", "--version"])

    assert result.exit_code == 0
    assert '"tool": "aria2c"' in result.output
    assert '"/opt/bin/aria2c"' in result.output
    assert '"--version"' in result.output


def test_missing_backend_reports_install_hint(monkeypatch) -> None:
    monkeypatch.setattr("atlas.passthrough.shutil.which", lambda _name: None)

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


def test_backend_plan_for_ytdlp_uses_python_module() -> None:
    plan = plan_backend_command(BackendTool.ytdlp, ["--version"], cwd=Path("/tmp"))

    assert plan.command[-3:] == ["-m", "yt_dlp", "--version"]
    assert plan.cwd == Path("/tmp")
    assert "shell is never used" in " ".join(plan.safety)
