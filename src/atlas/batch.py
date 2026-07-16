"""Batch URL processing."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass
from inspect import Parameter, signature
from pathlib import Path
from threading import Condition
from typing import Any
from urllib.parse import urlparse

from atlas.adaptive import AdaptiveScheduler
from atlas.errors import BatchError
from atlas.models import (
    BatchEntry,
    BatchItemResult,
    BatchKind,
    BatchSummary,
    DownloadResult,
    DownloadStatus,
)
from atlas.runner import ProcessControl

BatchHandler = Callable[[str], DownloadResult]
ProgressHook = Callable[[dict[str, Any]], None]
ConcurrentBatchHandler = Callable[..., DownloadResult]
BatchProgressHookFactory = Callable[[BatchEntry], ProgressHook]
BatchHostResolver = Callable[[BatchEntry], str | None]
_BACKOFF_STATUS_PATTERN = re.compile(r"\b(403|429|503)\b")


@dataclass(frozen=True)
class BatchItemContext:
    """Per-item runtime controls for operator-aware batch handlers."""

    entry: BatchEntry
    host: str | None
    process_control: ProcessControl


@dataclass(frozen=True)
class BatchOperatorResult:
    """Outcome of applying one operator key to batch controls."""

    key: str
    action: str
    applied: bool
    message: str
    snapshot: dict[str, object]


class BatchControl:
    """Thread-safe operator controls for queued and controllable active items."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._global_paused = False
        self._paused_hosts: set[str] = set()
        self._paused_lines: set[int] = set()
        self._cancel_all_reason: str | None = None
        self._canceled_lines: dict[int, str] = {}
        self._active_process_controls: dict[int, ProcessControl] = {}

    def pause_all(self) -> None:
        with self._condition:
            self._global_paused = True

    def resume_all(self) -> None:
        with self._condition:
            self._global_paused = False
            self._condition.notify_all()

    def pause_host(self, host: str) -> None:
        with self._condition:
            self._paused_hosts.add(host)

    def resume_host(self, host: str) -> None:
        with self._condition:
            self._paused_hosts.discard(host)
            self._condition.notify_all()

    def pause_line(self, line_no: int) -> None:
        with self._condition:
            self._paused_lines.add(line_no)

    def resume_line(self, line_no: int) -> None:
        with self._condition:
            self._paused_lines.discard(line_no)
            self._condition.notify_all()

    def cancel_all(self, reason: str = "canceled by operator") -> None:
        with self._condition:
            self._cancel_all_reason = reason
            for process_control in self._active_process_controls.values():
                process_control.cancel(reason)
            self._condition.notify_all()

    def cancel_line(self, line_no: int, reason: str = "canceled by operator") -> None:
        with self._condition:
            self._canceled_lines[line_no] = reason
            process_control = self._active_process_controls.get(line_no)
            if process_control is not None:
                process_control.cancel(reason)
            self._condition.notify_all()

    def wait_for_start(self, entry: BatchEntry, *, host: str | None = None) -> str | None:
        with self._condition:
            while True:
                reason = self._cancel_reason_locked(entry)
                if reason is not None:
                    return reason
                if (
                    not self._global_paused
                    and entry.line_no not in self._paused_lines
                    and (host is None or host not in self._paused_hosts)
                ):
                    return None
                self._condition.wait(timeout=0.1)

    def start_is_blocked(self, entry: BatchEntry, *, host: str | None = None) -> bool:
        """Return whether operator pause state currently blocks a pending item."""

        with self._condition:
            if self._cancel_reason_locked(entry) is not None:
                return False
            return (
                self._global_paused
                or entry.line_no in self._paused_lines
                or (host is not None and host in self._paused_hosts)
            )

    def wait_for_change(self, timeout: float = 0.1) -> None:
        """Wait briefly for an operator control state transition."""

        with self._condition:
            self._condition.wait(timeout=timeout)

    def register_active(
        self,
        entry: BatchEntry,
        process_control: ProcessControl,
    ) -> str | None:
        with self._condition:
            self._active_process_controls[entry.line_no] = process_control
            reason = self._cancel_reason_locked(entry)
            if reason is not None:
                process_control.cancel(reason)
            return reason

    def unregister_active(self, entry: BatchEntry) -> None:
        with self._condition:
            self._active_process_controls.pop(entry.line_no, None)

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "global_paused": self._global_paused,
                "paused_hosts": sorted(self._paused_hosts),
                "paused_lines": sorted(self._paused_lines),
                "canceled": self._cancel_all_reason is not None,
                "canceled_lines": sorted(self._canceled_lines),
                "active_lines": sorted(self._active_process_controls),
            }

    def _cancel_reason_locked(self, entry: BatchEntry) -> str | None:
        return self._canceled_lines.get(entry.line_no) or self._cancel_all_reason


class BatchOperatorController:
    """Apply shared TUI operator keys to a BatchControl instance."""

    def __init__(self, control: BatchControl) -> None:
        self.control = control

    def apply_key(
        self,
        key: str,
        *,
        focused_line: int | None = None,
        focused_host: str | None = None,
    ) -> BatchOperatorResult:
        if key == "g":
            return self._toggle_global_pause(key)
        if key == "h":
            return self._toggle_host_pause(key, focused_host)
        if key == "s":
            return self._toggle_line_pause(key, focused_line)
        if key == "x":
            return self._cancel_line(key, focused_line)
        if key == "X":
            self.control.cancel_all("canceled by operator")
            return self._result(key, "cancel_all", True, "cancel requested for all work")
        return self._result(key, "unknown", False, f"no batch operator action for {key!r}")

    def _toggle_global_pause(self, key: str) -> BatchOperatorResult:
        snapshot = self.control.snapshot()
        if snapshot["global_paused"]:
            self.control.resume_all()
            return self._result(key, "resume_all", True, "resumed queue starts")
        self.control.pause_all()
        return self._result(key, "pause_all", True, "paused new queue starts")

    def _toggle_host_pause(self, key: str, focused_host: str | None) -> BatchOperatorResult:
        if not focused_host:
            return self._result(key, "pause_host", False, "no focused host")
        snapshot = self.control.snapshot()
        paused_hosts_value = snapshot["paused_hosts"]
        paused_hosts = set(paused_hosts_value if isinstance(paused_hosts_value, list) else ())
        if focused_host in paused_hosts:
            self.control.resume_host(focused_host)
            return self._result(key, "resume_host", True, f"resumed host {focused_host}")
        self.control.pause_host(focused_host)
        return self._result(key, "pause_host", True, f"paused host {focused_host}")

    def _cancel_line(self, key: str, focused_line: int | None) -> BatchOperatorResult:
        if focused_line is None:
            return self._result(key, "cancel_line", False, "no focused item")
        self.control.cancel_line(focused_line, "canceled by operator")
        return self._result(key, "cancel_line", True, f"cancel requested for item {focused_line}")

    def _toggle_line_pause(self, key: str, focused_line: int | None) -> BatchOperatorResult:
        if focused_line is None:
            return self._result(key, "pause_line", False, "no focused item")
        snapshot = self.control.snapshot()
        active_lines_value = snapshot["active_lines"]
        active_lines = set(active_lines_value if isinstance(active_lines_value, list) else ())
        if focused_line in active_lines:
            return self._result(
                key,
                "pause_line",
                False,
                f"item {focused_line} is already active; cancel it instead",
            )
        paused_lines_value = snapshot["paused_lines"]
        paused_lines = set(paused_lines_value if isinstance(paused_lines_value, list) else ())
        if focused_line in paused_lines:
            self.control.resume_line(focused_line)
            return self._result(key, "resume_line", True, f"resumed item {focused_line}")
        self.control.pause_line(focused_line)
        return self._result(key, "pause_line", True, f"paused item {focused_line}")

    def _result(
        self,
        key: str,
        action: str,
        applied: bool,
        message: str,
    ) -> BatchOperatorResult:
        return BatchOperatorResult(
            key=key,
            action=action,
            applied=applied,
            message=message,
            snapshot=self.control.snapshot(),
        )


def load_batch_file(path: Path) -> tuple[list[BatchEntry], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BatchError(f"Could not read batch file {path}: {exc}") from exc

    entries: list[BatchEntry] = []
    skipped = 0
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            skipped += 1
            continue
        entries.append(BatchEntry(line_no=line_no, url=stripped))
    return entries, skipped


def run_batch(path: Path, kind: BatchKind, handler: BatchHandler) -> BatchSummary:
    """Run a batch sequentially with the legacy URL handler API."""

    def entry_handler(
        entry: BatchEntry,
        _progress_hooks: list[ProgressHook] | None,
    ) -> DownloadResult:
        return handler(entry.url)

    return run_batch_concurrent(path, kind, entry_handler, concurrency=1)


def run_batch_concurrent(
    path: Path,
    kind: BatchKind,
    handler: ConcurrentBatchHandler,
    *,
    concurrency: int,
    progress_hook_factory: BatchProgressHookFactory | None = None,
    per_host_concurrency: int | None = None,
    host_resolver: BatchHostResolver | None = None,
    control: BatchControl | None = None,
) -> BatchSummary:
    """Run a batch with bounded item-level concurrency."""

    if concurrency < 1:
        raise BatchError("Batch concurrency must be at least 1")
    if per_host_concurrency is not None and per_host_concurrency < 1:
        raise BatchError("Per-host concurrency must be at least 1")

    entries, skipped = load_batch_file(path)
    summary = BatchSummary(kind=kind, total=len(entries) + skipped, skipped=skipped)

    if not entries:
        return summary

    results: list[BatchItemResult] = []
    worker_count = min(concurrency, len(entries))
    unrestricted_queue = per_host_concurrency is None and control is None
    pending = list(reversed(entries)) if unrestricted_queue else list(entries)
    active: dict[Future[BatchItemResult], str] = {}
    active_by_host: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while pending or active:
            while pending and len(active) < worker_count:
                selected_index = (
                    len(pending) - 1
                    if unrestricted_queue
                    else _next_concurrent_entry_index(
                        pending,
                        active_by_host=active_by_host,
                        per_host_concurrency=per_host_concurrency,
                        host_resolver=host_resolver,
                        control=control,
                    )
                )
                if selected_index is None:
                    break
                entry = pending.pop(selected_index)
                host = _resolved_entry_host(entry, host_resolver) or "unknown"
                active_by_host[host] = active_by_host.get(host, 0) + 1
                try:
                    future = executor.submit(
                        _run_batch_entry,
                        entry,
                        handler,
                        progress_hook_factory,
                        control,
                        host,
                    )
                except BaseException:
                    _release_concurrent_host(active_by_host, host)
                    raise
                active[future] = host
            if not active:
                if control is not None and _all_pending_entries_paused(
                    pending,
                    host_resolver=host_resolver,
                    control=control,
                ):
                    control.wait_for_change()
                    continue
                raise BatchError("Batch scheduler could not start any pending URL")
            done, _not_done = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                host = active.pop(future)
                _release_concurrent_host(active_by_host, host)
                results.append(future.result())

    for item in sorted(results, key=lambda result: result.entry.line_no):
        _add_result(summary, item)
    return summary


def run_batch_adaptive(
    path: Path,
    kind: BatchKind,
    handler: ConcurrentBatchHandler,
    *,
    scheduler: AdaptiveScheduler,
    progress_hook_factory: BatchProgressHookFactory | None = None,
    host_resolver: BatchHostResolver | None = None,
    control: BatchControl | None = None,
) -> BatchSummary:
    """Run a batch with AIMD queue concurrency decisions while work is active."""

    entries, skipped = load_batch_file(path)
    summary = BatchSummary(kind=kind, total=len(entries) + skipped, skipped=skipped)
    if not entries:
        return summary

    pending = list(entries)
    results: list[BatchItemResult] = []
    max_workers = max(1, min(scheduler.global_max_concurrency, len(entries)))
    active: dict[Future[BatchItemResult], _AdaptiveBatchSlot] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while pending or active:
            _submit_adaptive_ready_items(
                pending,
                active,
                executor,
                handler,
                scheduler=scheduler,
                progress_hook_factory=progress_hook_factory,
                host_resolver=host_resolver,
                max_workers=max_workers,
                control=control,
            )
            if not active:
                if control is not None and _all_pending_entries_paused(
                    pending,
                    host_resolver=host_resolver,
                    control=control,
                ):
                    control.wait_for_change()
                    continue
                raise BatchError("Adaptive batch scheduler could not start any pending URL")
            done, _not_done = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                slot = active.pop(future)
                slot.release()
                result = future.result()
                results.append(result)
                _record_adaptive_batch_result(scheduler, result, host=slot.host)

    for item in sorted(results, key=lambda result: result.entry.line_no):
        _add_result(summary, item)
    return summary


def _next_concurrent_entry_index(
    entries: list[BatchEntry],
    *,
    active_by_host: dict[str, int],
    per_host_concurrency: int | None,
    host_resolver: BatchHostResolver | None,
    control: BatchControl | None,
) -> int | None:
    for index, entry in enumerate(entries):
        host = _resolved_entry_host(entry, host_resolver) or "unknown"
        if control is not None and control.start_is_blocked(entry, host=host):
            continue
        if per_host_concurrency is None or active_by_host.get(host, 0) < per_host_concurrency:
            return index
    return None


def _release_concurrent_host(active_by_host: dict[str, int], host: str) -> None:
    remaining = active_by_host[host] - 1
    if remaining:
        active_by_host[host] = remaining
    else:
        del active_by_host[host]


@dataclass
class _AdaptiveBatchSlot:
    context: AbstractContextManager[None]
    host: str | None

    def release(self) -> None:
        self.context.__exit__(None, None, None)


def _submit_adaptive_ready_items(
    pending: list[BatchEntry],
    active: dict[Future[BatchItemResult], _AdaptiveBatchSlot],
    executor: ThreadPoolExecutor,
    handler: ConcurrentBatchHandler,
    *,
    scheduler: AdaptiveScheduler,
    progress_hook_factory: BatchProgressHookFactory | None,
    host_resolver: BatchHostResolver | None,
    max_workers: int,
    control: BatchControl | None,
) -> None:
    target = max(1, min(scheduler.current_concurrency, max_workers))
    while pending and len(active) < target:
        selected_index = _next_adaptive_entry_index(
            pending,
            scheduler=scheduler,
            host_resolver=host_resolver,
            control=control,
        )
        if selected_index is None:
            return
        entry = pending.pop(selected_index)
        host = _resolved_entry_host(entry, host_resolver)
        context = scheduler.host_slot(host)
        try:
            context.__enter__()
            future = executor.submit(
                _run_batch_entry,
                entry,
                handler,
                progress_hook_factory,
                control,
                host,
            )
        except BaseException:
            context.__exit__(None, None, None)
            raise
        active[future] = _AdaptiveBatchSlot(context, host)


def _next_adaptive_entry_index(
    entries: list[BatchEntry],
    *,
    scheduler: AdaptiveScheduler,
    host_resolver: BatchHostResolver | None,
    control: BatchControl | None,
) -> int | None:
    for index, entry in enumerate(entries):
        host = _resolved_entry_host(entry, host_resolver)
        if control is not None and control.start_is_blocked(entry, host=host):
            continue
        if scheduler.can_start_for_host(host):
            return index
    return None


def _all_pending_entries_paused(
    entries: list[BatchEntry],
    *,
    host_resolver: BatchHostResolver | None,
    control: BatchControl,
) -> bool:
    return bool(entries) and all(
        control.start_is_blocked(
            entry,
            host=_resolved_entry_host(entry, host_resolver),
        )
        for entry in entries
    )


def _resolved_entry_host(
    entry: BatchEntry,
    host_resolver: BatchHostResolver | None,
) -> str | None:
    return (host_resolver(entry) if host_resolver else _entry_host(entry)) or "unknown"


def _record_adaptive_batch_result(
    scheduler: AdaptiveScheduler,
    result: BatchItemResult,
    *,
    host: str | None,
) -> None:
    if result.status == DownloadStatus.canceled:
        return
    if result.status in {DownloadStatus.success, DownloadStatus.dry_run, DownloadStatus.skipped}:
        scheduler.record_success(host=host)
        return
    status_code = _backoff_status_code(result.message)
    reason = _backoff_reason(result.message)
    scheduler.record_backoff(status_code=status_code, reason=reason, host=host)


def _backoff_status_code(message: str | None) -> int | None:
    if not message:
        return None
    match = _BACKOFF_STATUS_PATTERN.search(message)
    return int(match.group(1)) if match else None


def _backoff_reason(message: str | None) -> str | None:
    if not message:
        return None
    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "throttle" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return "throttle"
    if "retry" in lowered:
        return "high_retry_rate"
    if "disk" in lowered:
        return "disk_saturation"
    if "cpu" in lowered:
        return "cpu_bottleneck"
    if "postprocess" in lowered:
        return "postprocess_bottleneck"
    return None


def _run_batch_entry(
    entry: BatchEntry,
    handler: ConcurrentBatchHandler,
    progress_hook_factory: BatchProgressHookFactory | None,
    control: BatchControl | None = None,
    host: str | None = None,
) -> BatchItemResult:
    if control is not None:
        reason = control.wait_for_start(entry, host=host or _entry_host(entry))
        if reason is not None:
            return _canceled_batch_result(entry, reason)
    context = BatchItemContext(
        entry=entry,
        host=host or _entry_host(entry),
        process_control=ProcessControl(),
    )
    if control is not None:
        reason = control.register_active(entry, context.process_control)
        if reason is not None:
            control.unregister_active(entry)
            return _canceled_batch_result(entry, reason)
    progress_hooks = [progress_hook_factory(entry)] if progress_hook_factory else None
    try:
        result = _call_batch_handler(handler, entry, progress_hooks, context)
    except Exception as exc:
        if context.process_control.canceled:
            return _canceled_batch_result(entry, context.process_control.reason)
        return BatchItemResult(
            entry=entry,
            status=DownloadStatus.failed,
            message=str(exc),
        )
    finally:
        if control is not None:
            control.unregister_active(entry)
    if context.process_control.canceled:
        return _canceled_batch_result(entry, context.process_control.reason)
    return BatchItemResult(
        entry=entry,
        status=result.status,
        message=result.message,
        plan=result.ydl_opts,
    )


def _call_batch_handler(
    handler: ConcurrentBatchHandler,
    entry: BatchEntry,
    progress_hooks: list[ProgressHook] | None,
    context: BatchItemContext,
) -> DownloadResult:
    if _handler_accepts_context(handler):
        return handler(entry, progress_hooks, context)
    return handler(entry, progress_hooks)


def _handler_accepts_context(handler: ConcurrentBatchHandler) -> bool:
    try:
        parameters = signature(handler).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in parameters:
        if parameter.kind == Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in {
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional += 1
    return positional >= 3


def _canceled_batch_result(entry: BatchEntry, reason: str) -> BatchItemResult:
    return BatchItemResult(
        entry=entry,
        status=DownloadStatus.canceled,
        message=reason,
    )


def _entry_host(entry: BatchEntry) -> str | None:
    return urlparse(entry.url).hostname


def _add_result(summary: BatchSummary, result: BatchItemResult) -> None:
    if result.status in {DownloadStatus.success, DownloadStatus.dry_run}:
        summary.succeeded += 1
    elif result.status == DownloadStatus.canceled:
        summary.canceled += 1
        summary.skipped += 1
    elif result.status == DownloadStatus.skipped:
        summary.skipped += 1
    else:
        summary.failed += 1
    summary.results.append(result)
