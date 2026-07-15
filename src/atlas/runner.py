"""Safe subprocess helpers used for diagnostics, progress, and parity debugging."""

from __future__ import annotations

import codecs
import os
import selectors
import subprocess
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event, Lock


@dataclass(frozen=True)
class SubprocessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


LineCallback = Callable[[str], None]


# Streaming backends can emit huge, repetitive progress logs. Callers receive every
# parsed line through ``on_line``; keep only a useful diagnostic tail in the result.
_STREAM_OUTPUT_TAIL_LINES = 200


class ProcessCanceled(RuntimeError):
    """Raised when a controlled subprocess is canceled by the caller."""

    def __init__(self, command: Sequence[str], reason: str = "canceled") -> None:
        self.command = list(command)
        self.reason = reason
        super().__init__(f"Process canceled: {reason}")


class ProcessControl:
    """Small thread-safe cancellation handle for subprocess-backed engines."""

    def __init__(self, *, reason: str = "canceled by operator") -> None:
        self._cancel_requested = Event()
        self._reason = reason
        self._lock = Lock()

    def cancel(self, reason: str = "canceled by operator") -> None:
        with self._lock:
            self._reason = reason
        self._cancel_requested.set()

    @property
    def canceled(self) -> bool:
        return self._cancel_requested.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def raise_if_canceled(self, command: Sequence[str]) -> None:
        if self.canceled:
            raise ProcessCanceled(command, self.reason)


def run_args(
    args: Sequence[str],
    timeout: float | None = 15.0,
    *,
    control: ProcessControl | None = None,
) -> SubprocessResult:
    """Run a command without a shell and capture text output."""

    command = list(args)
    if control is not None:
        control.raise_if_canceled(command)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        text=True,
    )
    started = time.monotonic()
    try:
        while True:
            if control is not None and control.canceled:
                _terminate_process(process)
                raise ProcessCanceled(command, control.reason)
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                assert timeout is not None
                _terminate_process(process)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=stdout,
                    stderr=stderr,
                )
            poll_timeout = 0.1 if remaining is None else min(0.1, remaining)
            try:
                stdout, stderr = process.communicate(timeout=poll_timeout)
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            _terminate_process(process)
        raise
    return SubprocessResult(
        args=command,
        returncode=process.returncode if process.returncode is not None else process.wait(),
        stdout=stdout,
        stderr=stderr,
    )


def run_args_stream(
    args: Sequence[str],
    *,
    on_line: LineCallback,
    timeout: float | None = None,
    control: ProcessControl | None = None,
) -> SubprocessResult:
    """Run a command without a shell and stream output lines.

    ``on_line`` receives every non-empty line. The returned result retains the
    last 200 lines for failure diagnostics, preventing long-lived downloads from
    accumulating their entire backend log in memory.
    """

    command = list(args)
    if control is not None:
        control.raise_if_canceled(command)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("subprocess stdout pipe was not created")

    started = time.monotonic()
    lines: deque[str] = deque(maxlen=_STREAM_OUTPUT_TAIL_LINES)
    buffer: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    selector = selectors.DefaultSelector()
    stdout_fd = stdout.fileno()
    selector.register(stdout_fd, selectors.EVENT_READ)

    eof = False
    try:
        while not eof:
            if control is not None and control.canceled:
                _terminate_process(process)
                raise ProcessCanceled(command, control.reason)
            if timeout is not None and time.monotonic() - started > timeout:
                _terminate_process(process)
                raise subprocess.TimeoutExpired(command, timeout)

            if process.poll() is not None:
                ready = selector.select(timeout=0)
                if not ready:
                    break
            else:
                ready = selector.select(timeout=0.1)

            for _key, _events in ready:
                chunk = os.read(stdout_fd, 65_536)
                if chunk == b"":
                    eof = True
                    break
                _publish_stream_chunk(decoder.decode(chunk), buffer, lines, on_line)
    except BaseException:
        if process.poll() is None:
            _terminate_process(process)
        raise
    finally:
        selector.close()
        stdout.close()

    _publish_stream_chunk(decoder.decode(b"", final=True), buffer, lines, on_line)
    _publish_stream_line(buffer, lines, on_line)
    return SubprocessResult(
        args=command,
        returncode=process.wait(),
        stdout="\n".join(lines),
        stderr="",
    )


def _publish_stream_line(
    buffer: list[str],
    lines: deque[str],
    on_line: LineCallback,
) -> None:
    line = "".join(buffer).strip()
    buffer.clear()
    if not line:
        return
    lines.append(line)
    on_line(line)


def _publish_stream_chunk(
    chunk: str,
    buffer: list[str],
    lines: deque[str],
    on_line: LineCallback,
) -> None:
    """Split a decoded subprocess chunk into terminal-style progress lines."""

    for character in chunk:
        if character in {"\n", "\r"}:
            _publish_stream_line(buffer, lines, on_line)
        else:
            buffer.append(character)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
