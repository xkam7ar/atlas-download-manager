from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from atlas.runner import ProcessCanceled, ProcessControl, run_args, run_args_stream


def test_run_args_stream_finishes_after_eof() -> None:
    lines: list[str] = []

    result = run_args_stream(
        [sys.executable, "-c", "print('ready')"],
        on_line=lines.append,
        timeout=5,
    )

    assert result.returncode == 0
    assert lines == ["ready"]
    assert result.stdout == "ready"


def test_run_args_stream_delivers_all_lines_but_retains_diagnostic_tail() -> None:
    lines: list[str] = []
    total_lines = 250

    result = run_args_stream(
        [
            sys.executable,
            "-c",
            f"for index in range({total_lines}): print(f'line-{{index}}')",
        ],
        on_line=lines.append,
        timeout=5,
    )

    assert result.returncode == 0
    assert lines == [f"line-{index}" for index in range(total_lines)]
    assert result.stdout.splitlines() == lines[-200:]


def test_run_args_stream_splits_carriage_return_progress_lines() -> None:
    lines: list[str] = []

    result = run_args_stream(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('first\\rsecond\\nthird'); sys.stdout.flush()",
        ],
        on_line=lines.append,
        timeout=5,
    )

    assert result.returncode == 0
    assert lines == ["first", "second", "third"]
    assert result.stdout == "first\nsecond\nthird"


def test_run_args_uses_requested_working_directory(tmp_path: Path) -> None:
    result = run_args(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        timeout=5,
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert Path(result.stdout.strip()) == tmp_path


def test_run_args_replaces_invalid_utf8() -> None:
    result = run_args(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff')"],
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "�"


def test_run_args_stream_bounds_newline_free_diagnostic_lines() -> None:
    lines: list[str] = []
    result = run_args_stream(
        [sys.executable, "-c", "print('x' * 100_000, end='')"],
        on_line=lines.append,
        timeout=5,
    )

    assert result.returncode == 0
    assert "".join(lines) == "x" * 100_000
    assert max(map(len, lines)) <= 4_096
    assert len(result.stdout) <= 101_000


def test_run_args_stops_descendant_after_group_leader_exits(tmp_path: Path) -> None:
    marker = tmp_path / "unexpected-child-write"
    child = (
        f"import pathlib,time; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
    )
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"

    started = time.monotonic()
    result = run_args([sys.executable, "-c", parent], timeout=2)
    elapsed = time.monotonic() - started
    time.sleep(0.7)

    assert result.returncode == 0
    assert elapsed < 1.5
    assert not marker.exists()


def test_run_args_stream_stops_descendant_after_group_leader_exits(tmp_path: Path) -> None:
    marker = tmp_path / "unexpected-stream-child-write"
    child = (
        f"import pathlib,time; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); print('leader done')"
    )
    lines: list[str] = []

    result = run_args_stream(
        [sys.executable, "-c", parent],
        on_line=lines.append,
        timeout=2,
    )
    time.sleep(0.7)

    assert result.returncode == 0
    assert lines == ["leader done"]
    assert not marker.exists()


def test_run_args_refuses_to_start_when_canceled() -> None:
    control = ProcessControl()
    control.cancel("no start")

    with pytest.raises(ProcessCanceled, match="no start"):
        run_args(
            [sys.executable, "-c", "print('should not run')"],
            timeout=5,
            control=control,
        )


def test_run_args_can_cancel_running_process() -> None:
    control = ProcessControl()
    timer = threading.Timer(0.2, lambda: control.cancel("operator stop"))
    timer.start()

    try:
        with pytest.raises(ProcessCanceled, match="operator stop"):
            run_args(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=5,
                control=control,
            )
    finally:
        timer.cancel()


def test_run_args_stream_refuses_to_start_when_canceled() -> None:
    control = ProcessControl()
    control.cancel("no start")

    with pytest.raises(ProcessCanceled, match="no start"):
        run_args_stream(
            [sys.executable, "-c", "print('should not run')"],
            on_line=lambda _line: None,
            timeout=5,
            control=control,
        )


def test_run_args_stream_can_cancel_running_process() -> None:
    control = ProcessControl()
    lines: list[str] = []

    timer = threading.Timer(0.2, lambda: control.cancel("operator stop"))
    timer.start()

    try:
        with pytest.raises(ProcessCanceled, match="operator stop"):
            run_args_stream(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                on_line=lines.append,
                timeout=5,
                control=control,
            )
    finally:
        timer.cancel()

    assert lines == []
