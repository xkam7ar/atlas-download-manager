from __future__ import annotations

import pytest

from atlas.adaptive import (
    AdaptiveScheduler,
    classify_file_size,
    plan_items_from_site_scan,
    scan_site,
    work_item_from_probe,
)
from atlas.models import (
    AdaptivePoliteness,
    DirectFileProbe,
    EngineKind,
    FileSizeClass,
    HubKind,
    ProgressEvent,
    ProgressPhase,
    WorkBucket,
    WorkItem,
)
from atlas.network import FetchError, FetchErrorCode, FetchFailure, FetchResponse


def _item(
    url: str,
    size: int | None,
    *,
    ranges: bool = False,
    kind: HubKind = HubKind.file,
) -> WorkItem:
    return WorkItem(
        url=url,
        host="example.com",
        content_length=size,
        supports_ranges=ranges,
        size_class=classify_file_size(size),
        kind=kind,
    )


def _fetch_response(
    url: str,
    body: bytes,
    *,
    warnings: tuple[str, ...] = (),
    content_type: str = "text/html; charset=utf-8",
    body_truncated: bool = False,
) -> FetchResponse:
    return FetchResponse(
        url=url,
        final_url=url,
        status_code=200,
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        body=body,
        warnings=warnings,
        body_truncated=body_truncated,
    )


def test_scan_classification_boundaries() -> None:
    assert classify_file_size(100) == FileSizeClass.tiny
    assert classify_file_size(8 * 1024 * 1024) == FileSizeClass.small
    assert classify_file_size(64 * 1024 * 1024) == FileSizeClass.medium
    assert classify_file_size(700 * 1024 * 1024) == FileSizeClass.large
    assert classify_file_size(2 * 1024 * 1024 * 1024) == FileSizeClass.huge
    assert classify_file_size(None) == FileSizeClass.unknown


def test_work_item_from_probe_preserves_scan_metadata() -> None:
    probe = DirectFileProbe(
        url="https://example.com/download",
        final_url="https://cdn.example.com/file.zip",
        redirected=True,
        content_type="application/zip",
        content_length=20 * 1024 * 1024,
        content_disposition='attachment; filename="file.zip"',
        filename="file.zip",
        accept_ranges="bytes",
        supports_ranges=True,
        etag='"abc"',
        last_modified="Sun, 07 Jun 2026 10:00:00 GMT",
        file_extension=".zip",
        host="example.com",
        final_host="cdn.example.com",
        redirect_target="https://cdn.example.com/file.zip",
        discovered_links=["https://example.com/next"],
        sitemap_urls=["https://example.com/sitemap.xml"],
        robots_url="https://example.com/robots.txt",
        same_host=False,
        external_host=True,
    )

    item = work_item_from_probe(probe)

    assert item.host == "example.com"
    assert item.final_host == "cdn.example.com"
    assert item.redirect_target == "https://cdn.example.com/file.zip"
    assert item.content_disposition_filename == "file.zip"
    assert item.size_class == FileSizeClass.medium
    assert item.discovered_links == ["https://example.com/next"]
    assert item.sitemap_urls == ["https://example.com/sitemap.xml"]
    assert item.external_host is True


def test_scan_site_builds_smart_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <a href="/100/">100</a>
      <a href="/bbs/old-bbs-list.txt">bbs</a>
      <a href="/etext/index.html">etext</a>
      <a href="/pub/archive.zip">archive</a>
      <a href="/music/theme.mp3">theme</a>
      <a href="https://example.org/offsite.txt">external</a>
    </body></html>
    """

    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("http://textfiles.com/directory.html", html),
    )
    monkeypatch.setattr(
        "atlas.adaptive._robots_hints",
        lambda _url, *, timeout: ("http://textfiles.com/robots.txt", []),
    )

    scan = scan_site("http://textfiles.com/directory.html")

    assert scan.scan_type == "directory-style HTML index"
    assert scan.scan_counts == {
        "links": 6,
        "files": 2,
        "folders": 1,
        "html": 1,
        "media": 1,
        "external": 1,
        "same_host": 5,
    }
    assert scan.scan_recommended_mode == "Recursive directory mirror with HTML preservation"
    assert "bounded recursive mirror" in (scan.scan_recommended_strategy or "")
    assert scan.scan_estimated_bytes is not None
    assert len(scan.discovered_work_items) == 6

    item_by_url = {item.url: item for item in scan.discovered_work_items}
    text_item = item_by_url["http://textfiles.com/bbs/old-bbs-list.txt"]
    assert text_item.kind == HubKind.file
    assert text_item.size_class == FileSizeClass.tiny
    assert text_item.bucket == WorkBucket.tiny
    assert text_item.selected_backend == "native"

    archive_item = item_by_url["http://textfiles.com/pub/archive.zip"]
    assert archive_item.size_class == FileSizeClass.large
    assert "probe ranges" in (archive_item.scheduler_decision or "")

    external_item = item_by_url["https://example.org/offsite.txt"]
    assert external_item.external_host is True
    assert external_item.error == "external link skipped by default"


def test_scan_site_tls_failure_is_not_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))
    failure = FetchFailure(
        code=FetchErrorCode.tls_cert_verify_failed,
        message="TLS certificate verification failed",
        url="https://example.com/serveur/",
    )
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FetchError(failure)),
    )

    scan = scan_site("https://example.com/serveur/")

    assert scan.scan_status == "failed"
    assert scan.scan_counts["links"] == 0
    assert scan.scan_counts["folders"] == 0
    assert scan.scan_counts["files"] == 0
    assert scan.scan_errors[0]["code"] == "tls_failed"
    assert scan.error == "TLS certificate verification failed"


def test_scan_site_tls_failure_can_use_verified_backend_fetch_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"""
    <html><body>
      <a href="cours/">cours/</a>
      <a href="readme.txt">readme.txt</a>
    </body></html>
    """
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response(
            "https://example.com/serveur/",
            html,
            warnings=("Python TLS verification failed; scanned using curl fallback.",),
        ),
    )

    scan = scan_site("https://example.com/serveur/")

    assert scan.scan_status == "partial"
    assert scan.scan_counts["folders"] == 1
    assert scan.scan_counts["files"] == 1
    assert any("curl fallback" in warning for warning in scan.scan_warnings)
    assert scan.error is None


def test_scan_site_empty_document_is_empty_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("https://example.com/empty", b"<html></html>"),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/empty")

    assert scan.scan_status == "empty"
    assert scan.scan_counts["links"] == 0
    assert scan.scan_errors[0]["code"] == "no_links"
    assert scan.error is None


def test_scan_site_recognizes_copyparty_plain_text_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        b"# acct: *\n# perms: ['read', 'get']\n# srvinf: public archive\n"
        b"\x1b[36m20260626161231 95.7G ## Documentation/\x1b[0m\n"
        b"\x1b[36m20260626155850 638B README.md\x1b[0m\n"
    )
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response(
            "https://example.com/pub/",
            listing,
            content_type="text/plain; charset=utf-8",
        ),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/pub/")

    assert scan.scan_status == "success"
    assert scan.scan_type == "directory-style text index"
    assert scan.scan_counts["folders"] == 1
    assert scan.scan_counts["files"] == 1
    assert scan.discovered_links == [
        "https://example.com/pub/Documentation/",
        "https://example.com/pub/README.md",
    ]


def test_scan_site_rejects_ambiguous_plain_text_as_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response(
            "https://example.com/readme.txt",
            b"ordinary prose with no directory structure",
            content_type="text/plain",
        ),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/readme.txt")

    assert scan.scan_status == "failed"
    assert scan.scan_errors[0]["code"] == "parse_error"
    assert "not a recognized" in scan.scan_errors[0]["message"]
    assert scan.error == scan.scan_errors[0]["message"]


def test_scan_site_marks_link_limit_as_partial_and_omits_total_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "\n".join(
        f'<a href="file-{index}.txt">file {index}</a>' for index in range(2_001)
    ).encode()
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("https://example.com/files/", html),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/files/")

    assert scan.scan_status == "partial"
    assert scan.scan_counts["links"] == 2_000
    assert scan.scan_counts["complete"] == 0
    assert scan.scan_counts["links_truncated"] == 1
    assert scan.scan_estimated_bytes is None
    assert any("2,000" in warning and "partial" in warning for warning in scan.scan_warnings)


def test_scan_site_marks_body_limit_as_partial_and_omits_total_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b'<a href="one.txt">one</a>'
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response(
            "https://example.com/files/",
            html,
            body_truncated=True,
        ),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/files/")

    assert scan.scan_status == "partial"
    assert scan.scan_counts["complete"] == 0
    assert scan.scan_counts["body_truncated"] == 1
    assert scan.scan_estimated_bytes is None
    assert any("512 KiB" in warning and "partial" in warning for warning in scan.scan_warnings)


def test_scan_site_estimate_uses_visible_sizes_and_excludes_skipped_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"""
    <a href="../">Parent Directory</a> -
    <a href="one.txt">one</a> 10B
    <a href="https://other.example/two.txt">two</a> 20B
    """
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("https://example.com/files/", html),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/files/")

    assert scan.scan_estimated_bytes == 10
    assert scan.scan_counts["same_host"] == 1


def test_scan_site_warns_for_unbounded_query_and_parent_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_links = "\n".join(f'<a href="?page={index}">page {index}</a>' for index in range(10))
    html = f"""
    <html><body>
      <a href="../">Parent Directory</a>
      <a href="File.txt">File</a>
      <a href="file.txt">file</a>
      <a href="encoded%20name.txt">encoded</a>
      {query_links}
    </body></html>
    """.encode()

    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("https://example.com/pub/", html),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com/pub")

    assert any("this looks unbounded" in warning for warning in scan.scan_warnings)
    assert "Parent directory links detected and skipped by no-parent policy." in scan.scan_warnings
    assert "Case-sensitive duplicate paths detected; preserve folders to avoid collisions." in (
        scan.scan_warnings
    )
    assert "Query-based navigation detected; keep recursive depth bounded." in scan.scan_warnings
    assert "Input URL resolved to a trailing-slash directory." in scan.classification_notes

    parent_item = next(
        item for item in scan.discovered_work_items if item.url == "https://example.com/"
    )
    assert parent_item.error == "parent directory link skipped by no-parent policy"
    same_directory_item = next(
        item
        for item in scan.discovered_work_items
        if item.url == "https://example.com/pub/File.txt"
    )
    assert same_directory_item.error is None
    planned_urls = {item.url for item in plan_items_from_site_scan(scan, kind=HubKind.dir)}
    assert "https://example.com/" not in planned_urls
    assert "https://example.com/pub/File.txt" in planned_urls


def test_scan_site_enforces_canonical_origin_and_no_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"""
    <a href="inside.bin#fragment">inside</a>
    <a href="sub/../../sibling.bin">literal escape</a>
    <a href="%2e%2e/encoded.bin">encoded escape</a>
    <a href="/a/c/side.bin">sideways</a>
    <a href="//example.com/a/b/default.bin">default port</a>
    <a href="//example.com:444/a/b/other-port.bin">other port</a>
    <a href="//other.example/a/b/external.bin">external</a>
    <a href="java&#x73;cript:alert(1)">unsafe</a>
    """
    monkeypatch.setattr(
        "atlas.adaptive.FetchClient.get",
        lambda *_args, **_kwargs: _fetch_response("https://example.com:443/a/b/", html),
    )
    monkeypatch.setattr("atlas.adaptive._robots_hints", lambda _url, *, timeout: (None, []))

    scan = scan_site("https://example.com:443/a/b/")
    items = {item.url: item for item in scan.discovered_work_items}

    assert set(scan.discovered_links) == set(items)
    assert "https://example.com/a/b/inside.bin" in items
    assert "https://example.com/a/b/default.bin" in items
    assert items["https://example.com/a/sibling.bin"].error == (
        "parent directory link skipped by no-parent policy"
    )
    assert items["https://example.com/a/b/%2e%2e/encoded.bin"].error == (
        "parent directory link skipped by no-parent policy"
    )
    assert items["https://example.com/a/c/side.bin"].error == (
        "parent directory link skipped by no-parent policy"
    )
    assert items["https://example.com:444/a/b/other-port.bin"].error == (
        "external link skipped by default"
    )
    assert items["https://other.example/a/b/external.bin"].error == (
        "external link skipped by default"
    )
    planned_urls = {item.url for item in plan_items_from_site_scan(scan, kind=HubKind.dir)}
    assert planned_urls == {
        "https://example.com/a/b/inside.bin",
        "https://example.com/a/b/default.bin",
    }


def test_adaptive_ramp_up_and_down() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=10,
        politeness=AdaptivePoliteness.fast,
    )

    assert scheduler.current_concurrency == 4
    scheduler.record_success()
    assert scheduler.record_success() == 5
    assert scheduler.record_backoff(status_code=429) == 2


def test_throttle_backoff_on_local_bottleneck_sets_speed_limit() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=20,
        politeness=AdaptivePoliteness.aggressive,
    )

    new_limit = scheduler.record_backoff(reason="disk_saturation")

    assert new_limit == 4
    assert scheduler.current_speed_limit == "1M"


def test_per_host_caps_and_cancellation_cleanup() -> None:
    scheduler = AdaptiveScheduler(per_host_concurrency=1)

    with pytest.raises(RuntimeError), scheduler.host_slot("example.com"):
        assert scheduler.can_start_for_host("example.com") is False
        raise RuntimeError("cancelled")

    assert scheduler.can_start_for_host("example.com") is True
    with scheduler.host_slot("example.com"):
        assert scheduler.can_start_for_host("example.com") is False


def test_large_file_segment_selection() -> None:
    scheduler = AdaptiveScheduler(max_concurrency=100)

    plan = scheduler.plan(
        [_item("https://example.com/big.iso", 700 * 1024 * 1024, ranges=True)],
        kind=HubKind.file,
        backend="auto",
    )

    assert plan.queue_concurrency == 2
    assert plan.per_file_segments == 8
    assert plan.backend == "aria2"
    assert "large files" in plan.strategy


def test_total_connection_budget_clamps_queue_for_segmented_files() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=100,
        max_total_connections=8,
        politeness=AdaptivePoliteness.fast,
    )
    items = [
        _item(f"https://example.com/big-{index}.iso", 700 * 1024 * 1024, ranges=True)
        for index in range(4)
    ]

    plan = scheduler.plan(items, kind=HubKind.file, backend="auto")

    assert plan.per_file_segments == 8
    assert plan.queue_concurrency == 1
    assert plan.max_total_connections == 8
    assert plan.queue_concurrency * plan.per_file_segments <= plan.max_total_connections


def test_huge_file_without_ranges_disables_segments() -> None:
    scheduler = AdaptiveScheduler(max_concurrency=100)

    plan = scheduler.plan(
        [_item("https://example.com/big.iso", 4 * 1024 * 1024 * 1024, ranges=False)],
        kind=HubKind.file,
        backend="auto",
    )

    assert plan.queue_concurrency == 1
    assert plan.per_file_segments == 1
    assert "range splitting disabled" in "; ".join(plan.safety_notes)


def test_many_small_files_use_queue_concurrency_without_splitting() -> None:
    scheduler = AdaptiveScheduler(max_concurrency=100)
    items = [_item(f"https://example.com/{index}.txt", 64 * 1024) for index in range(10)]

    plan = scheduler.plan(items, kind=HubKind.file, backend="auto")

    assert plan.queue_concurrency == 10
    assert plan.per_file_segments == 1
    assert plan.backend == "native"
    assert "many small files" in plan.strategy


def test_plan_enriches_manifest_buckets_backends_and_priorities() -> None:
    scheduler = AdaptiveScheduler(max_concurrency=20)
    items = [
        _item("https://example.com/small.txt", 64 * 1024),
        WorkItem(url="https://youtu.be/abc", host="youtu.be", kind=HubKind.video),
        WorkItem(url="https://example.com/files/", host="example.com", kind=HubKind.dir),
    ]

    plan = scheduler.plan(items, kind=HubKind.file, backend="auto")

    assert plan.bucket_counts == {
        WorkBucket.tiny.value: 1,
        WorkBucket.media.value: 1,
        WorkBucket.recursive_mirror.value: 1,
    }
    item_by_url = {item.url: item for item in plan.work_items}
    assert item_by_url["https://example.com/small.txt"].priority == 10
    assert item_by_url["https://youtu.be/abc"].selected_backend == "yt-dlp"
    assert item_by_url["https://youtu.be/abc"].bucket == WorkBucket.media
    assert item_by_url["https://example.com/files/"].selected_backend == "wget2"
    assert item_by_url["https://example.com/files/"].recursion_depth == 0
    assert "recursive mirror" in item_by_url["https://example.com/files/"].scheduler_decision


def test_directory_mirrors_use_crawler_adaptive_strategy() -> None:
    scheduler = AdaptiveScheduler(max_concurrency=12)

    plan = scheduler.plan(
        [_item("https://example.com/files/", 64 * 1024, kind=HubKind.dir)],
        kind=HubKind.dir,
        backend="wget2",
    )

    assert plan.queue_concurrency == 2
    assert plan.per_file_segments == 1
    assert plan.backend == "wget2"
    assert plan.strategy == "crawler queue with per-host politeness"
    assert "bounded recursive queue" in "; ".join(plan.safety_notes)


def test_directory_scan_children_open_small_file_lane() -> None:
    seed = WorkItem(
        url="https://example.com/directory.html",
        host="example.com",
        kind=HubKind.dir,
        discovered_work_items=[
            WorkItem(
                url=f"https://example.com/files/{index}.txt",
                host="example.com",
                kind=HubKind.file,
                size_class=FileSizeClass.tiny,
                bucket=WorkBucket.tiny,
                same_host=True,
                probed=False,
            )
            for index in range(40)
        ],
    )

    plan = AdaptiveScheduler(
        max_concurrency=100,
        politeness=AdaptivePoliteness.fast,
    ).plan(plan_items_from_site_scan(seed, kind=HubKind.dir), kind=HubKind.dir, backend="auto")

    assert plan.queue_concurrency == 32
    assert plan.max_total_connections == 32
    assert plan.per_file_segments == 1
    assert "small-file lane" in plan.strategy


def test_unknown_transfer_reclassification_clamps_future_queue_starts() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=20,
        politeness=AdaptivePoliteness.normal,
        min_concurrency=1,
    )
    scheduler.current_concurrency = 8

    current = scheduler.record_transfer_classification(FileSizeClass.large)

    assert current == 2
    assert scheduler.current_concurrency == 2


def test_host_backoff_halves_only_the_erroring_host_cap() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=10,
        per_host_concurrency=6,
        politeness=AdaptivePoliteness.fast,
        min_concurrency=1,
    )

    before = scheduler.host_cap("busy.example")
    scheduler.record_backoff(status_code=503, host="busy.example")

    assert before == 6
    assert scheduler.host_cap("busy.example") == 3
    assert scheduler.host_cap("healthy.example") == 6
    assert scheduler.last_decision.action == "decrease"
    assert scheduler.last_decision.reason == "503"


def test_progress_samples_update_host_speed_evidence() -> None:
    scheduler = AdaptiveScheduler(
        max_concurrency=10,
        per_host_concurrency=4,
        politeness=AdaptivePoliteness.fast,
        min_concurrency=1,
    )

    decision = scheduler.observe_progress_event(
        ProgressEvent(
            engine=EngineKind.aria2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            url="https://speed.example/file.bin",
            downloaded_bytes=1024 * 1024,
            total_bytes=10 * 1024 * 1024,
            speed_bytes_per_sec=4_000_000,
            active_connections=4,
        )
    )
    stats = scheduler.host_stats("speed.example")

    assert decision.scope == "host:speed.example"
    assert stats.ewma_speed == 4_000_000
    assert stats.active_connections == 4
