from __future__ import annotations

import sys
import threading

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
