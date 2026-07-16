from __future__ import annotations

from pathlib import Path
from threading import Event, Lock
from time import sleep

from atlas.adaptive import AdaptiveScheduler
from atlas.batch import (
    BatchControl,
    BatchItemContext,
    BatchOperatorController,
    load_batch_file,
    run_batch,
    run_batch_adaptive,
    run_batch_concurrent,
)
from atlas.models import (
    AdaptivePoliteness,
    BatchEntry,
    BatchKind,
    DownloadResult,
    DownloadStatus,
)
from atlas.views import DEFAULT_OPERATOR_KEYMAP


def test_load_batch_file_skips_blanks_and_comments(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n# comment\nhttps://one.example\n  \nhttps://two.example\n",
        encoding="utf-8",
    )

    entries, skipped = load_batch_file(batch_file)

    assert skipped == 3
    assert [entry.url for entry in entries] == ["https://one.example", "https://two.example"]
    assert [entry.line_no for entry in entries] == [3, 5]


def test_run_batch_continues_after_failure(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://ok.example\nhttps://fail.example\n", encoding="utf-8")

    def handler(url: str) -> DownloadResult:
        if "fail" in url:
            raise RuntimeError("boom")
        return DownloadResult(status=DownloadStatus.dry_run, url=url, message="ok")

    summary = run_batch(batch_file, BatchKind.video, handler)

    assert summary.total == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert len(summary.results) == 2


def test_run_batch_total_includes_skipped_lines(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("# skip\nhttps://ok.example\n\n", encoding="utf-8")

    def handler(url: str) -> DownloadResult:
        return DownloadResult(status=DownloadStatus.success, url=url, message="ok")

    summary = run_batch(batch_file, BatchKind.audio, handler)

    assert summary.total == 3
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.skipped == 2


def test_run_batch_concurrent_starts_multiple_items(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
    started: list[int] = []
    lock = Lock()
    both_started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        with lock:
            started.append(entry.line_no)
            if len(started) == 2:
                both_started.set()
        assert both_started.wait(1)
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.video,
        handler,
        concurrency=2,
    )

    assert summary.succeeded == 2
    assert summary.failed == 0
    assert sorted(started) == [1, 2]


def test_run_batch_concurrent_delivers_progress_hooks(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\n", encoding="utf-8")

    def hook(_event: dict[str, object]) -> None:
        return None

    def handler(
        entry: BatchEntry,
        progress_hooks: list[object] | None,
    ) -> DownloadResult:
        assert entry.line_no == 1
        assert progress_hooks == [hook]
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.audio,
        handler,
        concurrency=4,
        progress_hook_factory=lambda _entry: hook,
    )

    assert summary.succeeded == 1


def test_run_batch_concurrent_can_cancel_pending_line(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
    control = BatchControl()
    control.cancel_line(2, "not this one")
    started: list[int] = []

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        started.append(entry.line_no)
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.file,
        handler,
        concurrency=1,
        control=control,
    )

    assert started == [1]
    assert summary.succeeded == 1
    assert summary.skipped == 1
    assert summary.canceled == 1
    assert summary.results[1].status == DownloadStatus.canceled
    assert summary.results[1].message == "not this one"


def test_run_batch_concurrent_passes_item_context(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example/file.txt\n", encoding="utf-8")
    seen: list[BatchItemContext] = []

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
        context: BatchItemContext,
    ) -> DownloadResult:
        seen.append(context)
        assert context.entry == entry
        assert context.host == "one.example"
        assert not context.process_control.canceled
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.file,
        handler,
        concurrency=1,
    )

    assert summary.succeeded == 1
    assert len(seen) == 1


def test_run_batch_concurrent_can_cancel_active_line(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example/file.txt\n", encoding="utf-8")
    control = BatchControl()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
        context: BatchItemContext,
    ) -> DownloadResult:
        assert control.snapshot()["active_lines"] == [1]
        control.cancel_line(entry.line_no, "stop active")
        assert context.process_control.canceled
        raise RuntimeError("backend should be masked by cancellation")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.file,
        handler,
        concurrency=1,
        control=control,
    )

    assert summary.succeeded == 0
    assert summary.failed == 0
    assert summary.canceled == 1
    assert summary.skipped == 1
    assert summary.results[0].status == DownloadStatus.canceled
    assert summary.results[0].message == "stop active"
    assert control.snapshot()["active_lines"] == []


def test_batch_operator_controller_applies_live_keys() -> None:
    control = BatchControl()
    controller = BatchOperatorController(control)

    paused = controller.apply_key("g")
    resumed = controller.apply_key("g")
    host_paused = controller.apply_key("h", focused_host="example.com")
    host_resumed = controller.apply_key("h", focused_host="example.com")
    line_paused = controller.apply_key("s", focused_line=12)
    line_resumed = controller.apply_key("s", focused_line=12)
    canceled = controller.apply_key("x", focused_line=12)
    canceled_all = controller.apply_key("X")

    assert paused.action == "pause_all"
    assert paused.applied is True
    assert paused.snapshot["global_paused"] is True
    assert resumed.action == "resume_all"
    assert resumed.snapshot["global_paused"] is False
    assert host_paused.action == "pause_host"
    assert host_paused.snapshot["paused_hosts"] == ["example.com"]
    assert host_resumed.action == "resume_host"
    assert host_resumed.snapshot["paused_hosts"] == []
    assert line_paused.action == "pause_line"
    assert line_paused.snapshot["paused_lines"] == [12]
    assert line_resumed.action == "resume_line"
    assert line_resumed.snapshot["paused_lines"] == []
    assert canceled.action == "cancel_line"
    assert canceled.snapshot["canceled_lines"] == [12]
    assert canceled_all.action == "cancel_all"
    assert canceled_all.snapshot["canceled"] is True


def test_batch_operator_controller_rejects_unfocused_actions() -> None:
    controller = BatchOperatorController(BatchControl())

    no_host = controller.apply_key("h")
    no_pause_item = controller.apply_key("s")
    no_item = controller.apply_key("x")
    unknown = controller.apply_key("z")

    assert no_host.applied is False
    assert no_host.message == "no focused host"
    assert no_pause_item.applied is False
    assert no_pause_item.message == "no focused item"
    assert no_item.applied is False
    assert no_item.message == "no focused item"
    assert unknown.applied is False
    assert unknown.action == "unknown"


def test_batch_operator_controller_keys_match_visible_keymap() -> None:
    visible_live_keys = {
        action.key for action in DEFAULT_OPERATOR_KEYMAP.actions if action.scope == "live"
    }

    assert {"g", "h", "s", "x", "X"}.issubset(visible_live_keys)


def test_run_batch_concurrent_waits_for_global_resume(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\n", encoding="utf-8")
    control = BatchControl()
    control.pause_all()
    started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    from threading import Thread

    result: list[object] = []
    worker = Thread(
        target=lambda: result.append(
            run_batch_concurrent(
                batch_file,
                BatchKind.file,
                handler,
                concurrency=1,
                control=control,
            )
        )
    )
    worker.start()
    sleep(0.05)
    assert not started.is_set()
    assert control.snapshot()["global_paused"] is True
    control.resume_all()
    worker.join(1)

    assert started.is_set()
    assert result
    assert isinstance(result[0], object)
    assert result[0].succeeded == 1


def test_run_batch_concurrent_waits_for_line_resume(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\n", encoding="utf-8")
    control = BatchControl()
    control.pause_line(1)
    started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    from threading import Thread

    result: list[object] = []
    worker = Thread(
        target=lambda: result.append(
            run_batch_concurrent(
                batch_file,
                BatchKind.file,
                handler,
                concurrency=1,
                control=control,
            )
        )
    )
    worker.start()
    sleep(0.05)
    assert not started.is_set()
    assert control.snapshot()["paused_lines"] == [1]
    control.resume_line(1)
    worker.join(1)

    assert started.is_set()
    assert result
    assert result[0].succeeded == 1


def test_run_batch_concurrent_enforces_per_host_cap(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://same.example/one\nhttps://same.example/two\nhttps://other.example/one\n",
        encoding="utf-8",
    )
    active_by_host: dict[str, int] = {}
    max_by_host: dict[str, int] = {}
    lock = Lock()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        host = entry.url.split("/", 3)[2]
        with lock:
            active_by_host[host] = active_by_host.get(host, 0) + 1
            max_by_host[host] = max(max_by_host.get(host, 0), active_by_host[host])
        sleep(0.05)
        with lock:
            active_by_host[host] -= 1
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.video,
        handler,
        concurrency=3,
        per_host_concurrency=1,
    )

    assert summary.succeeded == 3
    assert max_by_host["same.example"] == 1


def test_run_batch_concurrent_does_not_starve_other_hosts(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(
            [
                "https://busy.example/one",
                "https://busy.example/two",
                "https://busy.example/three",
                "https://healthy.example/one",
            ]
        ),
        encoding="utf-8",
    )
    healthy_started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if entry.line_no == 1:
            assert healthy_started.wait(0.5), "healthy host was starved by blocked busy workers"
        elif "healthy.example" in entry.url:
            healthy_started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.file,
        handler,
        concurrency=3,
        per_host_concurrency=1,
    )

    assert summary.succeeded == 4
    assert summary.failed == 0


def test_run_batch_concurrent_never_parks_workers_on_host_waiters(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(
            [
                *(f"https://busy.example/{index}" for index in range(3)),
                *(f"https://healthy.example/{index}" for index in range(3)),
                *(f"https://other.example/{index}" for index in range(3)),
            ]
        ),
        encoding="utf-8",
    )
    last_healthy_started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if entry.line_no == 1:
            assert last_healthy_started.wait(0.5), "host waiters occupied every worker"
        elif entry.line_no == 6:
            last_healthy_started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_concurrent(
        batch_file,
        BatchKind.file,
        handler,
        concurrency=3,
        per_host_concurrency=1,
    )

    assert summary.succeeded == 9
    assert summary.failed == 0


def test_run_batch_concurrent_skips_paused_line_without_blocking_queue(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
    control = BatchControl()
    control.pause_line(1)
    second_started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if entry.line_no == 2:
            second_started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    from threading import Thread

    summaries: list[object] = []
    worker = Thread(
        target=lambda: summaries.append(
            run_batch_concurrent(
                batch_file,
                BatchKind.file,
                handler,
                concurrency=1,
                control=control,
            )
        )
    )
    worker.start()
    try:
        assert second_started.wait(0.5), "paused line blocked a runnable line"
    finally:
        control.resume_line(1)
        worker.join(1)

    assert not worker.is_alive()
    assert summaries
    assert summaries[0].succeeded == 2


def test_run_batch_adaptive_ramps_up_runtime_starts(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(f"https://example.com/{index}.txt" for index in range(1, 5)),
        encoding="utf-8",
    )
    scheduler = AdaptiveScheduler(
        max_concurrency=3,
        per_host_concurrency=3,
        politeness=AdaptivePoliteness.normal,
        min_concurrency=1,
    )
    scheduler.current_concurrency = 1
    active_after_ramp = 0
    active = 0
    lock = Lock()
    both_late_items_started = Event()
    release_late_items = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        nonlocal active, active_after_ramp
        if entry.line_no <= 2:
            return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")
        with lock:
            active += 1
            active_after_ramp = max(active_after_ramp, active)
            if active == 2:
                both_late_items_started.set()
        assert both_late_items_started.wait(1)
        release_late_items.set()
        with lock:
            active -= 1
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_adaptive(
        batch_file,
        BatchKind.file,
        handler,
        scheduler=scheduler,
    )

    assert summary.succeeded == 4
    assert summary.failed == 0
    assert active_after_ramp == 2


def test_run_batch_adaptive_backoff_limits_new_runtime_starts(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(f"https://example.com/{index}.txt" for index in range(1, 4)),
        encoding="utf-8",
    )
    scheduler = AdaptiveScheduler(
        max_concurrency=3,
        per_host_concurrency=3,
        politeness=AdaptivePoliteness.normal,
        min_concurrency=1,
    )
    scheduler.current_concurrency = 2
    second_started = Event()
    first_can_fail = Event()
    third_started_before_second_finished = Event()
    release_second = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if entry.line_no == 1:
            assert second_started.wait(1)
            raise TimeoutError("timeout")
        if entry.line_no == 2:
            second_started.set()
            first_can_fail.set()
            assert release_second.wait(1)
            return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")
        if not release_second.is_set():
            third_started_before_second_finished.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    def release_when_first_can_fail() -> None:
        assert first_can_fail.wait(1)
        sleep(0.05)
        release_second.set()

    from threading import Thread

    releaser = Thread(target=release_when_first_can_fail)
    releaser.start()
    summary = run_batch_adaptive(
        batch_file,
        BatchKind.file,
        handler,
        scheduler=scheduler,
    )
    releaser.join(1)

    assert summary.succeeded == 2
    assert summary.failed == 1
    assert not third_started_before_second_finished.is_set()


def test_run_batch_adaptive_backoff_updates_erroring_host_cap(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(
            [
                "https://busy.example/one.txt",
                "https://busy.example/two.txt",
                "https://healthy.example/three.txt",
            ]
        ),
        encoding="utf-8",
    )
    scheduler = AdaptiveScheduler(
        max_concurrency=4,
        per_host_concurrency=4,
        politeness=AdaptivePoliteness.fast,
        min_concurrency=1,
    )

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if "busy.example/one" in entry.url:
            return DownloadResult(
                status=DownloadStatus.failed,
                url=entry.url,
                message="503 Service Unavailable",
            )
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    summary = run_batch_adaptive(
        batch_file,
        BatchKind.file,
        handler,
        scheduler=scheduler,
    )

    assert summary.succeeded == 2
    assert summary.failed == 1
    assert scheduler.host_cap("busy.example") == 2
    assert scheduler.host_cap("healthy.example") == 4


def test_run_batch_adaptive_attributes_backoff_to_resolved_host(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://origin.example/file.txt\n", encoding="utf-8")
    scheduler = AdaptiveScheduler(
        max_concurrency=4,
        per_host_concurrency=4,
        politeness=AdaptivePoliteness.fast,
        min_concurrency=1,
    )

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        return DownloadResult(
            status=DownloadStatus.failed,
            url=entry.url,
            message="503 Service Unavailable",
        )

    summary = run_batch_adaptive(
        batch_file,
        BatchKind.file,
        handler,
        scheduler=scheduler,
        host_resolver=lambda _entry: "cdn.example",
    )

    assert summary.failed == 1
    assert scheduler.host_cap("cdn.example") == 2
    assert scheduler.host_cap("origin.example") == 4


def test_run_batch_adaptive_skips_paused_host_without_blocking_queue(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://paused.example/one\nhttps://healthy.example/two\n",
        encoding="utf-8",
    )
    scheduler = AdaptiveScheduler(
        max_concurrency=1,
        per_host_concurrency=1,
        politeness=AdaptivePoliteness.normal,
        min_concurrency=1,
    )
    control = BatchControl()
    control.pause_host("paused.example")
    healthy_started = Event()

    def handler(
        entry: BatchEntry,
        _progress_hooks: list[object] | None,
    ) -> DownloadResult:
        if "healthy.example" in entry.url:
            healthy_started.set()
        return DownloadResult(status=DownloadStatus.success, url=entry.url, message="ok")

    from threading import Thread

    summaries: list[object] = []
    worker = Thread(
        target=lambda: summaries.append(
            run_batch_adaptive(
                batch_file,
                BatchKind.file,
                handler,
                scheduler=scheduler,
                control=control,
            )
        )
    )
    worker.start()
    try:
        assert healthy_started.wait(0.5), "paused host blocked a runnable host"
    finally:
        control.resume_host("paused.example")
        worker.join(1)

    assert not worker.is_alive()
    assert summaries
    assert summaries[0].succeeded == 2
