"""Adaptive scan and scheduling helpers for direct files and site mirrors."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlparse
from urllib.request import Request, urlopen

from atlas.directory_index import (
    DirectoryEntry,
    DirectoryIndex,
    UnsupportedDirectoryIndexError,
    decode_directory_body,
    is_directory_self_href,
    parse_directory_index,
    resolve_directory_href,
    same_http_origin,
    same_http_resource,
    url_within_directory_scope,
)
from atlas.file_probe import probe_direct_file, unprobed_direct_file, url_fingerprint
from atlas.models import (
    AdaptiveDownloadPlan,
    AdaptivePoliteness,
    DirectFileProbe,
    FileSizeClass,
    HubKind,
    ProgressEvent,
    ScanErrorCode,
    ScanStatus,
    WorkBucket,
    WorkItem,
)
from atlas.network import FetchClient, FetchError, FetchOptions, scan_error_code_from_fetch

TINY_FILE_BYTES = 256 * 1024
SMALL_FILE_BYTES = 16 * 1024 * 1024
MEDIUM_FILE_BYTES = 128 * 1024 * 1024
LARGE_FILE_BYTES = 1024 * 1024 * 1024

BACKOFF_STATUS_CODES = {403, 429, 503}
BACKOFF_REASONS = {
    "timeout",
    "high_retry_rate",
    "slowdown",
    "disk_saturation",
    "cpu_bottleneck",
    "postprocess_bottleneck",
    "throttle",
}
MAX_DISCOVERED_LINKS = 2000
_HTML_EXTENSIONS = {".html", ".htm", ".shtml", ".xhtml", ".php", ".asp", ".aspx", ".jsp"}
_TINY_FILE_EXTENSIONS = {
    ".txt",
    ".text",
    ".nfo",
    ".asc",
    ".md",
    ".rst",
    ".csv",
    ".json",
    ".xml",
    ".ini",
    ".cfg",
    ".log",
}
_SMALL_FILE_EXTENSIONS = {
    ".pdf",
    ".epub",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".css",
    ".js",
    ".ico",
    ".svg",
}
_LARGE_FILE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".7z",
    ".rar",
    ".iso",
    ".dmg",
}
_MEDIA_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".m4a",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
}
_DIRECT_FILE_EXTENSIONS = (
    _TINY_FILE_EXTENSIONS | _SMALL_FILE_EXTENSIONS | _LARGE_FILE_EXTENSIONS | _MEDIA_EXTENSIONS
)
_SCAN_ESTIMATE_BY_CLASS = {
    FileSizeClass.tiny: 64 * 1024,
    FileSizeClass.small: 2 * 1024 * 1024,
    FileSizeClass.medium: 64 * 1024 * 1024,
    FileSizeClass.large: 512 * 1024 * 1024,
    FileSizeClass.huge: 2 * 1024 * 1024 * 1024,
}


@dataclass(frozen=True)
class AdaptiveControls:
    """User-facing adaptive bounds and style."""

    enabled: bool = False
    max_concurrency: int = 100
    per_host_concurrency: int | None = None
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal
    dry_run: bool = False


@dataclass
class HostStats:
    """Runtime evidence used to tune one host independently."""

    host: str
    ewma_speed: float = 0.0
    ewma_latency: float | None = None
    active_items: int = 0
    active_connections: int = 0
    recent_errors: Counter[str] = field(default_factory=Counter)
    retry_rate: float = 0.0
    timeout_rate: float = 0.0
    last_backoff_at: float | None = None
    backoff_until: float | None = None
    current_cap: int = 1
    max_cap: int = 1
    stable_samples: int = 0


@dataclass(frozen=True)
class SpeedSample:
    """One normalized transfer sample consumed by the speed controller."""

    item_id: str
    host: str
    backend: str
    bucket: str
    downloaded_bytes: int
    speed_bytes_per_sec: float
    active_connections: int
    retry_count: int
    status: str
    timestamp: float


@dataclass(frozen=True)
class SchedulerDecision:
    """Explainable scheduler action generated from runtime evidence."""

    scope: str
    action: str
    reason: str
    previous_cap: int
    new_cap: int
    hold_seconds: float = 0.0
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _PolitenessPreset:
    start_concurrency: int
    per_host_concurrency: int
    per_file_segment_cap: int
    max_total_connections: int
    small_queue_cap: int
    medium_queue_cap: int
    large_queue_cap: int
    huge_queue_cap: int
    site_queue_cap: int


_PRESETS = {
    AdaptivePoliteness.normal: _PolitenessPreset(
        start_concurrency=2,
        per_host_concurrency=2,
        per_file_segment_cap=8,
        max_total_connections=32,
        small_queue_cap=12,
        medium_queue_cap=6,
        large_queue_cap=2,
        huge_queue_cap=1,
        site_queue_cap=4,
    ),
    AdaptivePoliteness.fast: _PolitenessPreset(
        start_concurrency=4,
        per_host_concurrency=4,
        per_file_segment_cap=16,
        max_total_connections=96,
        small_queue_cap=32,
        medium_queue_cap=10,
        large_queue_cap=3,
        huge_queue_cap=2,
        site_queue_cap=8,
    ),
    AdaptivePoliteness.aggressive: _PolitenessPreset(
        start_concurrency=8,
        per_host_concurrency=8,
        per_file_segment_cap=32,
        max_total_connections=160,
        small_queue_cap=64,
        medium_queue_cap=16,
        large_queue_cap=4,
        huge_queue_cap=2,
        site_queue_cap=16,
    ),
}


class AdaptiveSpeedController:
    """Evidence-driven AIMD controller for host-level transfer pressure."""

    def __init__(
        self,
        *,
        min_cap: int,
        max_cap: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.min_cap = max(1, min_cap)
        self.max_cap = max(self.min_cap, max_cap)
        self._clock = clock
        self._host_stats: dict[str, HostStats] = {}
        self.last_decision = SchedulerDecision(
            scope="global",
            action="stable",
            reason="initial adaptive budget",
            previous_cap=self.min_cap,
            new_cap=self.max_cap,
        )

    def host_cap(self, host: str | None) -> int:
        return self._stats(host).current_cap

    def host_stats(self, host: str | None) -> HostStats:
        stats = self._stats(host)
        return HostStats(
            host=stats.host,
            ewma_speed=stats.ewma_speed,
            ewma_latency=stats.ewma_latency,
            active_items=stats.active_items,
            active_connections=stats.active_connections,
            recent_errors=Counter(stats.recent_errors),
            retry_rate=stats.retry_rate,
            timeout_rate=stats.timeout_rate,
            last_backoff_at=stats.last_backoff_at,
            backoff_until=stats.backoff_until,
            current_cap=stats.current_cap,
            max_cap=stats.max_cap,
            stable_samples=stats.stable_samples,
        )

    def observe(self, sample: SpeedSample) -> SchedulerDecision:
        stats = self._stats(sample.host)
        previous_speed = stats.ewma_speed
        if sample.speed_bytes_per_sec > 0:
            stats.ewma_speed = (
                sample.speed_bytes_per_sec
                if previous_speed <= 0
                else (previous_speed * 0.7) + (sample.speed_bytes_per_sec * 0.3)
            )
        stats.active_connections = sample.active_connections
        stats.retry_rate = min(1.0, max(0.0, sample.retry_count / 5))
        status = sample.status.lower()
        if status in {"error", "failed", "retrying", "backoff"}:
            return self.backoff(sample.host, reason=status, evidence=_sample_evidence(sample))
        if stats.retry_rate >= 0.2:
            return self.backoff(
                sample.host,
                reason="high_retry_rate",
                evidence=_sample_evidence(sample) | {"retry_rate": stats.retry_rate},
            )
        if (
            previous_speed > 0
            and sample.speed_bytes_per_sec > 0
            and sample.speed_bytes_per_sec < previous_speed * 0.75
            and sample.active_connections >= stats.current_cap
        ):
            return self.backoff(
                sample.host,
                reason="speed stopped improving",
                evidence=_sample_evidence(sample) | {"ewma_speed": int(previous_speed)},
            )
        return self.success(sample.host, evidence=_sample_evidence(sample))

    def success(
        self,
        host: str | None,
        *,
        evidence: dict[str, object] | None = None,
    ) -> SchedulerDecision:
        stats = self._stats(host)
        now = self._clock()
        stats.stable_samples += 1
        if stats.backoff_until is not None and now < stats.backoff_until:
            self.last_decision = SchedulerDecision(
                scope=f"host:{stats.host}",
                action="hold",
                reason="backoff hold active",
                previous_cap=stats.current_cap,
                new_cap=stats.current_cap,
                hold_seconds=round(stats.backoff_until - now, 3),
                evidence=evidence or {},
            )
            return self.last_decision
        if stats.stable_samples >= 2 and stats.current_cap < stats.max_cap:
            previous = stats.current_cap
            stats.current_cap += 1
            stats.stable_samples = 0
            self.last_decision = SchedulerDecision(
                scope=f"host:{stats.host}",
                action="increase",
                reason="stable speed and low errors",
                previous_cap=previous,
                new_cap=stats.current_cap,
                evidence=evidence or {},
            )
            return self.last_decision
        self.last_decision = SchedulerDecision(
            scope=f"host:{stats.host}",
            action="stable",
            reason="collecting evidence",
            previous_cap=stats.current_cap,
            new_cap=stats.current_cap,
            evidence=evidence or {},
        )
        return self.last_decision

    def backoff(
        self,
        host: str | None,
        *,
        reason: str,
        evidence: dict[str, object] | None = None,
        hold_seconds: float = 30.0,
    ) -> SchedulerDecision:
        stats = self._stats(host)
        previous = stats.current_cap
        stats.current_cap = max(self.min_cap, max(1, stats.current_cap // 2))
        stats.stable_samples = 0
        stats.last_backoff_at = self._clock()
        stats.backoff_until = stats.last_backoff_at + hold_seconds
        stats.recent_errors.update([reason])
        self.last_decision = SchedulerDecision(
            scope=f"host:{stats.host}",
            action="decrease",
            reason=reason,
            previous_cap=previous,
            new_cap=stats.current_cap,
            hold_seconds=hold_seconds,
            evidence=evidence or {},
        )
        return self.last_decision

    def _stats(self, host: str | None) -> HostStats:
        key = host or "unknown"
        stats = self._host_stats.get(key)
        if stats is None:
            stats = HostStats(
                host=key,
                current_cap=self.max_cap,
                max_cap=self.max_cap,
            )
            self._host_stats[key] = stats
        return stats


def _sample_evidence(sample: SpeedSample) -> dict[str, object]:
    return {
        "backend": sample.backend,
        "bucket": sample.bucket,
        "downloaded_bytes": sample.downloaded_bytes,
        "speed_bytes_per_sec": int(sample.speed_bytes_per_sec),
        "active_connections": sample.active_connections,
        "retry_count": sample.retry_count,
        "status": sample.status,
    }


class AdaptiveScheduler:
    """AIMD-style adaptive scheduler state and plan builder."""

    def __init__(
        self,
        *,
        max_concurrency: int = 100,
        per_host_concurrency: int | None = None,
        politeness: AdaptivePoliteness = AdaptivePoliteness.normal,
        min_concurrency: int = 2,
        max_total_connections: int | None = None,
    ) -> None:
        self.global_min_concurrency = max(1, min(min_concurrency, 100))
        self.global_max_concurrency = max(
            self.global_min_concurrency,
            min(max_concurrency, 100),
        )
        self.politeness = politeness
        self._preset = _PRESETS[politeness]
        self.per_host_concurrency = min(
            per_host_concurrency or self._preset.per_host_concurrency,
            self.global_max_concurrency,
        )
        self.per_file_segment_cap = self._preset.per_file_segment_cap
        self.max_total_connections = max(
            1,
            min(max_total_connections or self._preset.max_total_connections, 6400),
        )
        self.current_concurrency = min(
            max(self._preset.start_concurrency, self.global_min_concurrency),
            self.global_max_concurrency,
        )
        self.current_speed_limit: str | None = None
        self._success_streak = 0
        self._active_by_host: dict[str, int] = {}
        self._speed_controller = AdaptiveSpeedController(
            min_cap=1,
            max_cap=self.per_host_concurrency,
        )
        self._lock = threading.Lock()

    def plan(
        self,
        work_items: Iterable[WorkItem],
        *,
        kind: HubKind,
        backend: str,
    ) -> AdaptiveDownloadPlan:
        items = list(work_items)
        if not items:
            items = [WorkItem(url="", kind=kind, probed=False, error="no scanned items")]
        largest = _largest_size_class(items)
        queue_concurrency = self._queue_concurrency(items, kind=kind, largest=largest)
        per_file_segments = self._segments_for(items, largest=largest)
        per_file_segments = min(per_file_segments, self.max_total_connections)
        queue_concurrency = self._clamp_queue_to_connection_budget(
            queue_concurrency,
            per_file_segments,
        )
        selected_backend = self._backend_for(backend, items, per_file_segments)
        strategy = self._strategy_for(items, kind=kind, largest=largest, segments=per_file_segments)
        items = [
            _enrich_work_item(
                item,
                selected_backend=selected_backend,
                strategy=strategy,
            )
            for item in items
        ]
        size_counts = Counter(item.size_class.value for item in items)
        bucket_counts = Counter((item.bucket or _bucket_for_item(item)).value for item in items)
        hosts = Counter(item.host or item.final_host or "unknown" for item in items)
        safety_notes = self._safety_notes(items, kind=kind)
        total_connections = max(
            1,
            min(self.max_total_connections, queue_concurrency * per_file_segments),
        )
        per_host_connections = max(
            1,
            min(
                self.max_total_connections,
                min(self.per_host_concurrency, queue_concurrency) * per_file_segments,
            ),
        )
        postprocessor_cap = (
            1 if any(item.kind in {HubKind.video, HubKind.audio} for item in items) else 0
        )
        return AdaptiveDownloadPlan(
            enabled=True,
            politeness=self.politeness,
            global_min_concurrency=self.global_min_concurrency,
            global_max_concurrency=self.global_max_concurrency,
            queue_concurrency=queue_concurrency,
            per_host_concurrency=min(self.per_host_concurrency, queue_concurrency),
            per_file_segments=per_file_segments,
            per_file_segment_cap=self.per_file_segment_cap,
            max_active_files=min(queue_concurrency, max(1, len(items))),
            max_total_connections=total_connections,
            max_per_host_connections=per_host_connections,
            max_active_postprocessors=postprocessor_cap,
            speed_limit=self.current_speed_limit,
            backend=selected_backend,
            strategy=strategy,
            size_counts=dict(size_counts),
            bucket_counts=dict(bucket_counts),
            hosts=dict(hosts),
            work_items=items,
            safety_notes=safety_notes,
        )

    def record_success(self, *, host: str | None = None) -> int:
        """AIMD additive increase after stable successful progress."""

        self._speed_controller.success(host)
        self._success_streak += 1
        if self._success_streak >= 2 and self.current_concurrency < self.global_max_concurrency:
            self.current_concurrency += 1
            self._success_streak = 0
        return self.current_concurrency

    def record_backoff(
        self,
        *,
        status_code: int | None = None,
        reason: str | None = None,
        retry_rate: float | None = None,
        host: str | None = None,
    ) -> int:
        """AIMD multiplicative decrease for throttling and local bottlenecks."""

        should_backoff = (
            status_code in BACKOFF_STATUS_CODES
            or reason in BACKOFF_REASONS
            or (retry_rate is not None and retry_rate >= 0.2)
        )
        if not should_backoff:
            return self.current_concurrency
        backoff_reason = reason or (str(status_code) if status_code else "backoff")
        self._speed_controller.backoff(
            host,
            reason=backoff_reason,
            evidence={
                "status_code": status_code,
                "retry_rate": retry_rate,
            },
        )
        self._success_streak = 0
        self.current_concurrency = max(
            self.global_min_concurrency,
            max(1, self.current_concurrency // 2),
        )
        if reason in {"disk_saturation", "cpu_bottleneck", "postprocess_bottleneck"}:
            self.current_speed_limit = "1M"
        return self.current_concurrency

    def observe_progress_event(
        self,
        event: ProgressEvent,
        *,
        host: str | None = None,
        backend: str | None = None,
        bucket: WorkBucket | None = None,
    ) -> SchedulerDecision:
        """Feed normalized progress evidence into the host-level controller."""

        sample = SpeedSample(
            item_id=event.item_id or event.backend_id or event.url or "unknown",
            host=host or _host(event.url or "") or "unknown",
            backend=backend or event.engine.value,
            bucket=(bucket or event.work_bucket or WorkBucket.unknown).value,
            downloaded_bytes=event.downloaded_bytes or 0,
            speed_bytes_per_sec=event.speed_bytes_per_sec or 0.0,
            active_connections=event.active_connections or 0,
            retry_count=event.retry_count or 0,
            status=event.status,
            timestamp=monotonic(),
        )
        decision = self._speed_controller.observe(sample)
        if decision.action == "decrease":
            self.current_concurrency = max(
                self.global_min_concurrency,
                min(self.current_concurrency, decision.new_cap),
            )
        return decision

    def host_cap(self, host: str | None) -> int:
        return self._speed_controller.host_cap(host)

    def host_stats(self, host: str | None) -> HostStats:
        return self._speed_controller.host_stats(host)

    @property
    def last_decision(self) -> SchedulerDecision:
        return self._speed_controller.last_decision

    def record_transfer_classification(self, size_class: FileSizeClass) -> int:
        """Clamp live starts after an unknown-size transfer reveals its real class."""

        cap = {
            FileSizeClass.huge: self._preset.huge_queue_cap,
            FileSizeClass.large: self._preset.large_queue_cap,
            FileSizeClass.medium: self._preset.medium_queue_cap,
        }.get(size_class)
        if cap is not None:
            self.current_concurrency = max(
                self.global_min_concurrency,
                min(self.current_concurrency, cap, self.global_max_concurrency),
            )
        return self.current_concurrency

    def can_start_for_host(self, host: str | None) -> bool:
        key = host or "unknown"
        with self._lock:
            return self._active_by_host.get(key, 0) < self.host_cap(key)

    @contextmanager
    def host_slot(self, host: str | None) -> Iterator[None]:
        key = host or "unknown"
        with self._lock:
            active = self._active_by_host.get(key, 0)
            if active >= self.host_cap(key):
                msg = f"per-host concurrency reached for {key}"
                raise RuntimeError(msg)
            self._active_by_host[key] = active + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._active_by_host.get(key, 1) - 1
                if remaining > 0:
                    self._active_by_host[key] = remaining
                else:
                    self._active_by_host.pop(key, None)

    def _queue_concurrency(
        self,
        items: list[WorkItem],
        *,
        kind: HubKind,
        largest: FileSizeClass,
    ) -> int:
        if kind in {HubKind.site, HubKind.dir}:
            if _many_small_files(items):
                cap = self._preset.small_queue_cap
            elif largest == FileSizeClass.huge:
                cap = self._preset.huge_queue_cap
            elif largest == FileSizeClass.large:
                cap = self._preset.large_queue_cap
            elif largest == FileSizeClass.medium:
                cap = self._preset.medium_queue_cap
            else:
                cap = self._preset.site_queue_cap
        elif _many_small_files(items):
            cap = self._preset.small_queue_cap
        elif largest == FileSizeClass.huge:
            cap = self._preset.huge_queue_cap
        elif largest == FileSizeClass.large:
            cap = self._preset.large_queue_cap
        elif largest == FileSizeClass.medium:
            cap = self._preset.medium_queue_cap
        else:
            cap = self._preset.small_queue_cap if len(items) > 1 else self.current_concurrency
        desired = max(len(items), self.global_min_concurrency)
        return max(1, min(cap, self.global_max_concurrency, desired))

    def _clamp_queue_to_connection_budget(self, queue: int, segments: int) -> int:
        per_item_connections = max(1, segments)
        connection_limited_queue = max(1, self.max_total_connections // per_item_connections)
        return max(1, min(queue, connection_limited_queue))

    def _segments_for(self, items: list[WorkItem], *, largest: FileSizeClass) -> int:
        ranged = any(item.supports_ranges for item in items)
        if not ranged:
            return 1
        if largest == FileSizeClass.huge:
            return min(16, self.per_file_segment_cap)
        if largest == FileSizeClass.large:
            return min(8, self.per_file_segment_cap)
        if largest == FileSizeClass.medium:
            return min(3, self.per_file_segment_cap)
        return 1

    def _backend_for(self, backend: str, items: list[WorkItem], per_file_segments: int) -> str:
        if backend != "auto":
            return backend
        if per_file_segments > 1:
            return "aria2"
        if len(items) > 1 and _many_small_files(items):
            return "native"
        return "auto"

    def _strategy_for(
        self,
        items: list[WorkItem],
        *,
        kind: HubKind,
        largest: FileSizeClass,
        segments: int,
    ) -> str:
        if kind in {HubKind.site, HubKind.dir}:
            if _many_small_files(items):
                return (
                    "recursive mirror: small-file lane, "
                    "high queue concurrency, no per-file splitting"
                )
            if largest in {FileSizeClass.large, FileSizeClass.huge}:
                return (
                    "recursive mirror: large-file lane, "
                    "few active jobs, ranged segments when supported"
                )
            return "crawler queue with per-host politeness"
        if _many_small_files(items):
            return "many small files: queue concurrency, no per-file splitting"
        if largest == FileSizeClass.huge:
            return "huge files: 1-2 active files, ranged segments when supported"
        if largest == FileSizeClass.large:
            return "large files: low queue concurrency with ranged segments"
        if largest == FileSizeClass.medium:
            if segments > 1:
                return "medium files: moderate queue concurrency with a few segments"
            return "medium files: moderate queue concurrency, native download"
        if largest in {FileSizeClass.tiny, FileSizeClass.small}:
            return "small files: keep-alive/native queue, no per-file splitting"
        return "unknown sizes: conservative queue, no speculative splitting"

    def _safety_notes(self, items: list[WorkItem], *, kind: HubKind) -> list[str]:
        notes = [
            f"politeness={self.politeness.value}",
            "AIMD backoff on 429/403/503/timeouts/retry spikes/local bottlenecks",
        ]
        if kind in {HubKind.site, HubKind.dir}:
            notes.append("bounded recursive queue; robots and sitemap hints scanned when available")
        if any(item.external_host for item in items):
            notes.append("external-host redirects or links found; per-host caps still apply")
        if any(item.size_class == FileSizeClass.unknown for item in items):
            notes.append("unknown-size items stay conservative until progress data arrives")
        if not any(item.supports_ranges for item in items):
            notes.append("range splitting disabled because range support was not reported")
        return notes


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_name = "href" if tag in {"a", "link"} else "src" if tag in {"img", "script"} else ""
        if not attr_name:
            return
        for name, value in attrs:
            if name.lower() == attr_name and value:
                resolved = resolve_directory_href(self.base_url, value)
                if (
                    resolved is not None
                    and not same_http_resource(self.base_url, resolved)
                    and not is_directory_self_href(self.base_url, value, resolved)
                ):
                    self.links.append(resolved)


def classify_file_size(content_length: int | None) -> FileSizeClass:
    if content_length is None:
        return FileSizeClass.unknown
    if content_length < TINY_FILE_BYTES:
        return FileSizeClass.tiny
    if content_length < SMALL_FILE_BYTES:
        return FileSizeClass.small
    if content_length < MEDIUM_FILE_BYTES:
        return FileSizeClass.medium
    if content_length < LARGE_FILE_BYTES:
        return FileSizeClass.large
    return FileSizeClass.huge


def work_item_from_probe(probe: DirectFileProbe, *, kind: HubKind = HubKind.file) -> WorkItem:
    checksum_metadata = _checksum_metadata_from_probe(probe)
    return WorkItem(
        url=probe.url,
        host=probe.host or _host(probe.url),
        final_url=probe.final_url,
        final_host=probe.final_host or _host(probe.final_url),
        redirect_target=probe.redirect_target,
        kind=kind,
        content_type=probe.content_type,
        content_length=probe.content_length,
        content_disposition=probe.content_disposition,
        content_disposition_filename=probe.filename,
        filename=probe.filename,
        file_extension=probe.file_extension,
        accept_ranges=probe.accept_ranges,
        supports_ranges=probe.supports_ranges,
        etag=probe.etag,
        last_modified=probe.last_modified,
        discovered_links=probe.discovered_links,
        sitemap_urls=probe.sitemap_urls,
        robots_url=probe.robots_url,
        url_fingerprint=probe.url_fingerprint,
        mirror_fingerprint=probe.mirror_fingerprint,
        classification_notes=probe.classification_notes,
        warning_flags=probe.warning_flags,
        same_host=probe.same_host,
        external_host=probe.external_host,
        size_class=classify_file_size(probe.content_length),
        checksum_metadata=checksum_metadata,
        probed=probe.probed,
        error=probe.error,
    )


def scan_direct_file(url: str, *, dry_run: bool = False) -> WorkItem:
    probe = (
        unprobed_direct_file(url, reason="dry run: probe skipped")
        if dry_run
        else probe_direct_file(url)
    )
    return work_item_from_probe(probe, kind=HubKind.file)


def scan_site(url: str, *, dry_run: bool = False, timeout: float = 10.0) -> WorkItem:
    if dry_run:
        probe = unprobed_direct_file(url, reason="dry run: site scan skipped")
        return work_item_from_probe(probe, kind=HubKind.site).model_copy(
            update={
                "scan_type": "dry-run scan",
                "scan_recommended_mode": "Scan skipped; use configured mirror policy",
                "scan_recommended_strategy": "dry run: no network probe performed",
                "scan_counts": {
                    "links": 0,
                    "files": 0,
                    "folders": 0,
                    "html": 0,
                    "media": 0,
                    "external": 0,
                },
                "scan_warnings": ["dry run: recursive warning analysis skipped"],
            }
        )
    try:
        response = FetchClient().get(
            url,
            FetchOptions(timeout=timeout, user_agent="atlas/0.1"),
            fallback_tools=True,
        )
    except FetchError as exc:
        probe = unprobed_direct_file(url, reason=str(exc))
        failure = exc.failure
        return work_item_from_probe(probe, kind=HubKind.site).model_copy(
            update={
                "scan_type": "failed scan",
                "scan_recommended_mode": "Scan failed before discovery",
                "scan_recommended_strategy": "retry, run doctor, or continue as backend mirror",
                "scan_counts": {
                    "links": 0,
                    "files": 0,
                    "folders": 0,
                    "html": 0,
                    "media": 0,
                    "external": 0,
                    "same_host": 0,
                },
                "scan_status": ScanStatus.failed,
                "scan_errors": [
                    {
                        "code": scan_error_code_from_fetch(failure.code).value,
                        "message": failure.message,
                        "url": failure.url,
                        "recoverable": failure.recoverable,
                    }
                ],
            }
        )

    final_url = response.final_url
    headers = response.headers
    content_type = headers.get("Content-Type")
    body = response.body

    links, html_links_truncated = _extract_links_with_status(
        final_url or url,
        body,
        content_type,
    )
    parser_error: str | None = None
    try:
        directory_index = parse_directory_index(
            final_url or url,
            body,
            content_type=content_type,
        )
    except UnsupportedDirectoryIndexError as exc:
        parser_error = str(exc)
        directory_index = DirectoryIndex(
            source_url=final_url or url,
            host=_host(final_url or url),
            entries=(),
            parser_name="unsupported-text",
            complete=False,
        )
    if directory_index.entries and not links:
        links = [entry.url for entry in directory_index.entries]
    links_truncated = html_links_truncated or directory_index.truncated_reason == "entry-limit"
    incomplete_scan = response.body_truncated or links_truncated
    robots_url, sitemap_urls = _robots_hints(final_url or url, timeout=timeout)
    source_host = _host(url)
    final_host = _host(final_url)
    external = bool(source_host and final_host and source_host != final_host)
    discovered_items = (
        _discovered_work_items_from_directory_entries(final_url or url, directory_index.entries)
        if directory_index.entries
        else _discovered_work_items(final_url or url, links)
    )
    counts = _scan_counts(links, discovered_items)
    scan_warnings = _scan_warnings(
        seed_url=url,
        final_url=final_url or url,
        links=links,
        items=discovered_items,
        content_type=content_type,
        parser_name=directory_index.parser_name,
    )
    if response.body_truncated:
        scan_warnings.append(
            "Scan body exceeded 512 KiB; results are partial and safety totals are incomplete."
        )
    if links_truncated:
        scan_warnings.append(
            "Link discovery exceeded 2,000 unique links; results are partial and "
            "safety totals are incomplete."
        )
    scan_warnings = [*response.warnings, *scan_warnings]
    if incomplete_scan:
        counts["complete"] = 0
        if response.body_truncated:
            counts["body_truncated"] = 1
        if links_truncated:
            counts["links_truncated"] = 1
    scan_type = (
        "unsupported text directory index"
        if parser_error
        else "directory-style text index"
        if directory_index.parser_name == "copyparty-text"
        else "directory-style CopyParty HTML index"
        if directory_index.parser_name == "copyparty-html"
        else "directory-style HTML index"
        if directory_index.parser_name == "autoindex-html"
        else _scan_type(url=final_url or url, content_type=content_type, counts=counts)
    )
    recommended_mode = (
        "Directory scan unavailable for this text response"
        if parser_error
        else _recommended_mode(scan_type=scan_type, counts=counts)
    )
    recommended_strategy = (
        "download as a direct file or use a supported directory index"
        if parser_error
        else _recommended_strategy(scan_type=scan_type, counts=counts)
    )
    estimated_bytes = None if incomplete_scan else _scan_estimated_bytes(discovered_items)
    no_links = counts["links"] == 0 and counts["same_host"] == 0
    scan_errors = (
        [
            {
                "code": ScanErrorCode.parse_error.value,
                "message": parser_error,
                "url": final_url or url,
                "recoverable": True,
            }
        ]
        if parser_error
        else [
            {
                "code": ScanErrorCode.no_links.value,
                "message": "No links found in fetched document",
                "url": final_url or url,
                "recoverable": True,
            }
        ]
        if no_links
        else []
    )
    scan_status = (
        ScanStatus.failed
        if parser_error
        else ScanStatus.partial
        if incomplete_scan or response.warnings
        else ScanStatus.empty
        if no_links
        else ScanStatus.success
    )
    item = WorkItem(
        url=url,
        host=source_host,
        final_url=final_url,
        final_host=final_host,
        redirect_target=final_url if final_url and final_url != url else None,
        kind=HubKind.site,
        content_type=content_type,
        content_length=_content_length(headers.get("Content-Length")),
        content_disposition=headers.get("Content-Disposition"),
        content_disposition_filename=None,
        filename=None,
        file_extension=None,
        accept_ranges=headers.get("Accept-Ranges"),
        supports_ranges=(headers.get("Accept-Ranges") or "").lower() == "bytes",
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
        discovered_links=links,
        discovered_work_items=discovered_items,
        sitemap_urls=sitemap_urls,
        robots_url=robots_url,
        url_fingerprint=url_fingerprint(final_url or url),
        mirror_fingerprint=url_fingerprint(final_url or url),
        classification_notes=_site_classification_notes(
            seed_url=url,
            final_url=final_url or url,
            content_type=content_type,
        ),
        warning_flags=[],
        same_host=not external,
        external_host=external or any(_host(link) != source_host for link in links if _host(link)),
        scan_type=scan_type,
        scan_recommended_mode=recommended_mode,
        scan_recommended_strategy=recommended_strategy,
        scan_counts=counts,
        scan_estimated_bytes=estimated_bytes,
        scan_warnings=scan_warnings,
        scan_status=scan_status,
        scan_errors=scan_errors,
        size_class=classify_file_size(_content_length(headers.get("Content-Length"))),
        recursion_depth=0,
        checksum_metadata=_checksum_metadata_from_headers(
            etag=headers.get("ETag"),
            last_modified=headers.get("Last-Modified"),
        ),
        error=parser_error,
    )
    return item


def default_adaptive_controls(
    *,
    enabled: bool,
    max_concurrency: int | None,
    per_host_concurrency: int | None,
    politeness: AdaptivePoliteness,
    dry_run: bool,
) -> AdaptiveControls:
    return AdaptiveControls(
        enabled=enabled,
        max_concurrency=max_concurrency or 100,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        dry_run=dry_run,
    )


def build_plan_for_direct_file(
    url: str,
    *,
    controls: AdaptiveControls,
    backend: str,
) -> AdaptiveDownloadPlan:
    item = scan_direct_file(url, dry_run=controls.dry_run)
    return AdaptiveScheduler(
        max_concurrency=controls.max_concurrency,
        per_host_concurrency=controls.per_host_concurrency,
        politeness=controls.politeness,
    ).plan([item], kind=HubKind.file, backend=backend)


def build_plan_for_site(
    url: str,
    *,
    controls: AdaptiveControls,
    backend: str,
) -> AdaptiveDownloadPlan:
    item = scan_site(url, dry_run=controls.dry_run)
    items = _plan_items_from_scan(item, kind=HubKind.site)
    return AdaptiveScheduler(
        max_concurrency=controls.max_concurrency,
        per_host_concurrency=controls.per_host_concurrency,
        politeness=controls.politeness,
    ).plan(items, kind=HubKind.site, backend=backend)


def build_plan_for_items(
    items: Iterable[WorkItem],
    *,
    controls: AdaptiveControls,
    kind: HubKind,
    backend: str,
) -> AdaptiveDownloadPlan:
    return AdaptiveScheduler(
        max_concurrency=controls.max_concurrency,
        per_host_concurrency=controls.per_host_concurrency,
        politeness=controls.politeness,
    ).plan(items, kind=kind, backend=backend)


def plan_items_from_site_scan(seed: WorkItem, *, kind: HubKind) -> list[WorkItem]:
    """Return same-host scanned children that should influence adaptive planning."""

    return _plan_items_from_scan(seed, kind=kind)


def _extract_links(base_url: str, body: bytes, content_type: str | None) -> list[str]:
    links, _truncated = _extract_links_with_status(base_url, body, content_type)
    return links


def _extract_links_with_status(
    base_url: str,
    body: bytes,
    content_type: str | None,
) -> tuple[list[str], bool]:
    if content_type and "html" not in content_type.lower():
        return [], False
    parser = _LinkParser(base_url)
    try:
        parser.feed(decode_directory_body(body, content_type))
    except ValueError:
        return [], False
    links = _dedupe(parser.links)
    return links[:MAX_DISCOVERED_LINKS], len(links) > MAX_DISCOVERED_LINKS


def _discovered_work_items(seed_url: str, links: list[str]) -> list[WorkItem]:
    items: list[WorkItem] = []
    for link in links:
        host = _host(link)
        same_host = same_http_origin(seed_url, link)
        parent_skipped = same_host and _is_parent_directory_link(seed_url, link)
        extension = _extension_from_url(link)
        kind = _kind_for_link(link, extension=extension)
        size_class = _estimated_size_class_for_extension(extension, kind=kind)
        bucket = _bucket_for_scan_item(kind, size_class)
        error = None
        if not same_host:
            error = "external link skipped by default"
        elif parent_skipped:
            error = "parent directory link skipped by no-parent policy"
        items.append(
            WorkItem(
                url=link,
                host=host,
                final_host=host,
                kind=kind,
                file_extension=extension,
                url_fingerprint=url_fingerprint(link),
                mirror_fingerprint=url_fingerprint(link),
                classification_notes=_link_classification_notes(link, kind=kind),
                warning_flags=_link_warning_flags(link),
                same_host=same_host,
                external_host=not same_host,
                size_class=size_class,
                bucket=bucket,
                selected_backend=_selected_backend_for_scan_item(kind),
                priority=_priority_for_bucket(bucket),
                recursion_depth=1,
                scheduler_decision=(
                    "parent directory link skipped by no-parent policy"
                    if parent_skipped
                    else _scan_decision(kind, bucket, same_host=same_host)
                ),
                probed=False,
                error=error,
            )
        )
    return items


def _discovered_work_items_from_directory_entries(
    seed_url: str,
    entries: Iterable[DirectoryEntry],
) -> list[WorkItem]:
    items: list[WorkItem] = []
    for entry in entries:
        host = _host(entry.url)
        same_host = same_http_origin(seed_url, entry.url)
        parent_skipped = entry.parent or (
            same_host and _is_parent_directory_link(seed_url, entry.url)
        )
        kind = _kind_for_directory_entry(entry)
        size_class = classify_file_size(entry.visible_size)
        if size_class == FileSizeClass.unknown:
            size_class = _estimated_size_class_for_extension(entry.extension, kind=kind)
        bucket = _bucket_for_scan_item(kind, size_class)
        error = entry.skipped_reason
        if error is None and not same_host:
            error = "external link skipped by default"
        elif error is None and parent_skipped:
            error = "parent directory link skipped by no-parent policy"
        notes = [
            "visible directory index row",
            f"entry kind: {entry.kind}",
        ]
        if entry.parent:
            notes.append("parent directory entry")
        if entry.visible_size is not None:
            notes.append("visible size from index")
        if entry.last_modified is not None:
            notes.append("visible modification time from index")
        items.append(
            WorkItem(
                url=entry.url,
                host=host,
                final_host=host,
                kind=kind,
                filename=entry.name,
                content_type=entry.content_type,
                content_length=entry.visible_size,
                last_modified=(
                    entry.last_modified.isoformat(timespec="seconds")
                    if entry.last_modified is not None
                    else None
                ),
                file_extension=entry.extension,
                url_fingerprint=url_fingerprint(entry.url),
                mirror_fingerprint=url_fingerprint(entry.url),
                classification_notes=notes,
                warning_flags=_link_warning_flags(entry.url),
                same_host=same_host,
                external_host=not same_host,
                size_class=size_class,
                bucket=bucket,
                selected_backend=_selected_backend_for_scan_item(kind),
                priority=_priority_for_bucket(bucket),
                recursion_depth=entry.depth + 1,
                scheduler_decision=(
                    "parent directory link skipped by no-parent policy"
                    if parent_skipped
                    else _scan_decision(kind, bucket, same_host=same_host)
                ),
                probed=False,
                error=error,
            )
        )
    return items


def _kind_for_directory_entry(entry: DirectoryEntry) -> HubKind:
    if entry.kind == "directory":
        return HubKind.dir
    if entry.kind == "html":
        return HubKind.site
    extension = entry.extension or _extension_from_url(entry.url)
    return _kind_for_link(entry.url, extension=extension)


def _plan_items_from_scan(seed: WorkItem, *, kind: HubKind) -> list[WorkItem]:
    discovered = [
        item.model_copy(update={"kind": HubKind.dir})
        if kind == HubKind.dir and item.kind == HubKind.site
        else item
        for item in seed.discovered_work_items
        if item.same_host and not item.external_host and item.error is None
    ]
    if discovered:
        return discovered
    return [seed.model_copy(update={"kind": kind})]


def _scan_counts(links: list[str], items: list[WorkItem]) -> dict[str, int]:
    same_host_items = [item for item in items if item.same_host and item.error is None]
    folders = sum(1 for item in same_host_items if item.kind == HubKind.dir)
    html = sum(1 for item in same_host_items if item.kind == HubKind.site)
    media = sum(1 for item in same_host_items if item.kind in {HubKind.audio, HubKind.video})
    files = sum(1 for item in same_host_items if item.kind == HubKind.file)
    external = sum(1 for item in items if item.external_host)
    return {
        "links": len(links),
        "files": files,
        "folders": folders,
        "html": html,
        "media": media,
        "external": external,
        "same_host": len(same_host_items),
    }


def _scan_warnings(
    *,
    seed_url: str,
    final_url: str,
    links: list[str],
    items: list[WorkItem],
    content_type: str | None,
    parser_name: str,
) -> list[str]:
    warnings: list[str] = []
    if _missing_trailing_slash_redirect(seed_url, final_url):
        warnings.append(
            "Missing trailing slash resolved by redirect; "
            "Atlas will plan from the final folder URL."
        )
    if _looks_unbounded(links):
        warnings.append(
            "Scan warning: this looks unbounded; review depth, scope, "
            "and reject rules before recursive download."
        )
    if any(item.error == "parent directory link skipped by no-parent policy" for item in items):
        warnings.append("Parent directory links detected and skipped by no-parent policy.")
    if _has_encoded_or_spaced_paths(links):
        warnings.append("URL-encoded or spaced filenames detected; source URLs are preserved.")
    if _has_case_sensitive_duplicates(links):
        warnings.append(
            "Case-sensitive duplicate paths detected; preserve folders to avoid collisions."
        )
    if _has_query_navigation(links):
        warnings.append("Query-based navigation detected; keep recursive depth bounded.")
    external_count = sum(1 for item in items if item.external_host)
    if external_count >= 10 or (links and external_count / max(1, len(links)) >= 0.25):
        warnings.append(
            "Many external links were skipped by default; keep same-host unless intentional."
        )
    if (
        content_type
        and "html" not in content_type.lower()
        and links
        and parser_name != "copyparty-text"
    ):
        warnings.append(
            "Non-HTML content produced links; verify parser assumptions before mirroring."
        )
    return _dedupe(warnings)


def _site_classification_notes(
    *,
    seed_url: str,
    final_url: str,
    content_type: str | None,
) -> list[str]:
    notes: list[str] = []
    if urlparse(seed_url).scheme == "http" and urlparse(final_url).scheme == "https":
        notes.append("Redirected from HTTP to HTTPS.")
    if _missing_trailing_slash_redirect(seed_url, final_url):
        notes.append("Input URL resolved to a trailing-slash directory.")
    if content_type and "html" not in content_type.lower():
        notes.append("Site scan received non-HTML content; link discovery may be empty.")
    return notes


def _link_classification_notes(url: str, *, kind: HubKind) -> list[str]:
    notes: list[str] = []
    parsed = urlparse(url)
    if parsed.query and kind in {HubKind.dir, HubKind.site}:
        notes.append("Query-based navigation candidate.")
    if " " in parsed.path or unquote(parsed.path) != parsed.path:
        notes.append("URL path contains spaces or encoded characters.")
    return notes


def _link_warning_flags(url: str) -> list[str]:
    flags: list[str] = []
    parsed = urlparse(url)
    if parsed.query and _navigation_query_keys(url):
        flags.append("query_navigation")
    if " " in parsed.path:
        flags.append("space_in_path")
    if unquote(parsed.path) != parsed.path:
        flags.append("url_encoded_path")
    return flags


def _scan_type(*, url: str, content_type: str | None, counts: dict[str, int]) -> str:
    extension = _extension_from_url(url)
    if extension in _MEDIA_EXTENSIONS:
        return "media link"
    if extension in _DIRECT_FILE_EXTENSIONS - _MEDIA_EXTENSIONS:
        return "direct file"
    if content_type and "html" not in content_type.lower():
        return "non-HTML resource"
    folders = counts.get("folders", 0)
    if folders and folders >= counts.get("html", 0):
        return "directory-style HTML index"
    if counts.get("html", 0) >= counts.get("files", 0):
        return "HTML page"
    return "mixed link page"


def _recommended_mode(*, scan_type: str, counts: dict[str, int]) -> str:
    if "directory" in scan_type and "index" in scan_type:
        return "Recursive directory mirror with HTML preservation"
    if scan_type == "media link" or counts.get("media", 0) > max(counts.get("files", 0), 0):
        return "Media extraction with yt-dlp"
    if scan_type == "direct file":
        return "Direct file download"
    if scan_type == "HTML page":
        return "Offline website copy with page requisites"
    if counts.get("folders", 0) or counts.get("files", 0) >= counts.get("html", 0):
        return "Recursive directory mirror with HTML preservation"
    return "Offline website copy with page requisites"


def _recommended_strategy(*, scan_type: str, counts: dict[str, int]) -> str:
    files = counts.get("files", 0)
    folders = counts.get("folders", 0)
    html = counts.get("html", 0)
    media = counts.get("media", 0)
    if "directory" in scan_type and "index" in scan_type:
        return "bounded recursive mirror: same-host/no-parent safety with adaptive queue lanes"
    if media and media >= files:
        return "route media URLs to yt-dlp; keep postprocessing budget separate"
    if scan_type == "HTML page":
        return "offline website copy: preserve page requisites and keep traversal bounded"
    if files >= 50 and html <= files:
        return "adaptive small-file lane: high queue concurrency, low per-file threading"
    if folders or html:
        return "bounded recursive mirror: same-host/no-parent safety with adaptive queue lanes"
    return "cautious adaptive queue until early transfer data reclassifies unknown sizes"


def _scan_estimated_bytes(items: list[WorkItem]) -> int | None:
    estimates = [
        (
            item.content_length
            if item.content_length is not None
            else _SCAN_ESTIMATE_BY_CLASS[item.size_class]
        )
        for item in items
        if (
            item.same_host
            and not item.external_host
            and item.error is None
            and item.size_class in _SCAN_ESTIMATE_BY_CLASS
        )
    ]
    if not estimates:
        return None
    return sum(estimates)


def _kind_for_link(url: str, *, extension: str | None) -> HubKind:
    parsed = urlparse(url)
    path = parsed.path or ""
    if path.endswith("/"):
        return HubKind.dir
    if extension in _MEDIA_EXTENSIONS:
        video_extensions = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
        return HubKind.video if extension in video_extensions else HubKind.audio
    if extension in _HTML_EXTENSIONS or not extension:
        return HubKind.site
    return HubKind.file


def _estimated_size_class_for_extension(
    extension: str | None,
    *,
    kind: HubKind,
) -> FileSizeClass:
    if kind in {HubKind.site, HubKind.dir}:
        return FileSizeClass.tiny
    if extension in _TINY_FILE_EXTENSIONS:
        return FileSizeClass.tiny
    if extension in _SMALL_FILE_EXTENSIONS:
        return FileSizeClass.small
    if extension in _MEDIA_EXTENSIONS:
        return FileSizeClass.large
    if extension in _LARGE_FILE_EXTENSIONS:
        return FileSizeClass.large
    return FileSizeClass.unknown


def _bucket_for_scan_item(kind: HubKind, size_class: FileSizeClass) -> WorkBucket:
    if kind in {HubKind.audio, HubKind.video}:
        return WorkBucket.media
    if kind in {HubKind.site, HubKind.dir}:
        return WorkBucket.recursive_mirror
    return WorkBucket(size_class.value)


def _selected_backend_for_scan_item(kind: HubKind) -> str:
    if kind in {HubKind.audio, HubKind.video}:
        return "yt-dlp"
    if kind in {HubKind.site, HubKind.dir}:
        return "wget2"
    return "native"


def _scan_decision(kind: HubKind, bucket: WorkBucket, *, same_host: bool) -> str:
    if not same_host:
        return "external link: skipped unless span-host policy is enabled"
    if bucket == WorkBucket.recursive_mirror:
        return "recursive candidate: bounded by same-host/no-parent policy"
    if bucket == WorkBucket.media:
        return "media candidate: route to yt-dlp when selected"
    if bucket in {WorkBucket.tiny, WorkBucket.small}:
        return "small discovered file: high queue concurrency, one stream"
    if bucket in {WorkBucket.large, WorkBucket.huge}:
        return "large discovered file: fewer active jobs, probe ranges before splitting"
    return f"{kind.value}: conservative queue until probed"


def _is_parent_directory_link(seed_url: str, link: str) -> bool:
    return same_http_origin(seed_url, link) and not url_within_directory_scope(seed_url, link)


def _directory_scope(path: str) -> str:
    if not path or path == "/":
        return "/"
    if path.endswith("/"):
        return path
    directory = path.rsplit("/", 1)[0]
    return f"{directory}/" if directory else "/"


def _missing_trailing_slash_redirect(seed_url: str, final_url: str) -> bool:
    seed = urlparse(seed_url)
    final = urlparse(final_url)
    return (
        seed.hostname == final.hostname
        and bool(seed.path)
        and not seed.path.endswith("/")
        and final.path == f"{seed.path}/"
    )


def _looks_unbounded(links: list[str]) -> bool:
    navigation_links = [link for link in links if _navigation_query_keys(link)]
    unbounded_path_tokens = {"/calendar", "/tag", "/tags", "/search"}
    calendarish = [
        link
        for link in links
        if any(token in (urlparse(link).path or "").lower() for token in unbounded_path_tokens)
    ]
    return len(navigation_links) >= 8 or len(calendarish) >= 4


def _has_encoded_or_spaced_paths(links: list[str]) -> bool:
    return any(
        " " in urlparse(link).path or unquote(urlparse(link).path) != urlparse(link).path
        for link in links
    )


def _has_case_sensitive_duplicates(links: list[str]) -> bool:
    seen: dict[str, str] = {}
    for link in links:
        parsed = urlparse(link)
        key = f"{parsed.hostname or ''}{parsed.path}".lower()
        existing = seen.get(key)
        if existing and existing != parsed.path:
            return True
        seen[key] = parsed.path
    return False


def _has_query_navigation(links: list[str]) -> bool:
    return any(_navigation_query_keys(link) for link in links)


def _navigation_query_keys(url: str) -> set[str]:
    navigation_keys = {
        "calendar",
        "day",
        "dir",
        "folder",
        "month",
        "offset",
        "p",
        "page",
        "q",
        "query",
        "s",
        "search",
        "start",
        "tag",
        "year",
    }
    parsed = urlparse(url)
    return {
        key.lower()
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() in navigation_keys
    }


def _extension_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if "." not in path.rsplit("/", 1)[-1]:
        return None
    suffix = "." + path.rsplit(".", 1)[-1].lower()
    return suffix if len(suffix) > 1 else None


def _robots_hints(url: str, *, timeout: float) -> tuple[str | None, list[str]]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None, []
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    request = Request(robots_url, headers={"User-Agent": "atlas/0.1"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(128 * 1024).decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError):
        return robots_url, []
    sitemaps: list[str] = []
    for line in body.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "sitemap":
            sitemaps.append(value.strip())
    return robots_url, _dedupe(sitemaps)


def _many_small_files(items: list[WorkItem]) -> bool:
    if len(items) < 4:
        return False
    return all(item.size_class in {FileSizeClass.tiny, FileSizeClass.small} for item in items)


def _enrich_work_item(
    item: WorkItem,
    *,
    selected_backend: str,
    strategy: str,
) -> WorkItem:
    bucket = item.bucket or _bucket_for_item(item)
    priority = item.priority if item.priority != 100 else _priority_for_bucket(bucket)
    backend = item.selected_backend or _selected_backend_for_item(item, selected_backend)
    recursion_depth = item.recursion_depth
    if recursion_depth is None and item.kind in {HubKind.site, HubKind.dir}:
        recursion_depth = 0
    decision = item.scheduler_decision or _decision_for_item(
        item,
        bucket=bucket,
        selected_backend=backend,
        strategy=strategy,
    )
    updates: dict[str, object] = {
        "bucket": bucket,
        "priority": priority,
        "selected_backend": backend,
        "scheduler_decision": decision,
    }
    if recursion_depth is not None:
        updates["recursion_depth"] = recursion_depth
    return item.model_copy(update=updates)


def _bucket_for_item(item: WorkItem) -> WorkBucket:
    if item.kind in {HubKind.video, HubKind.audio}:
        return WorkBucket.media
    if item.kind in {HubKind.site, HubKind.dir}:
        return WorkBucket.recursive_mirror
    return WorkBucket(item.size_class.value)


def _priority_for_bucket(bucket: WorkBucket) -> int:
    return {
        WorkBucket.tiny: 10,
        WorkBucket.small: 20,
        WorkBucket.medium: 30,
        WorkBucket.large: 40,
        WorkBucket.huge: 50,
        WorkBucket.unknown: 60,
        WorkBucket.media: 70,
        WorkBucket.recursive_mirror: 80,
    }[bucket]


def _selected_backend_for_item(item: WorkItem, selected_backend: str) -> str:
    if item.kind in {HubKind.video, HubKind.audio}:
        return "yt-dlp"
    if item.kind in {HubKind.site, HubKind.dir}:
        return selected_backend if selected_backend != "auto" else "wget2"
    return selected_backend


def _decision_for_item(
    item: WorkItem,
    *,
    bucket: WorkBucket,
    selected_backend: str,
    strategy: str,
) -> str:
    if bucket == WorkBucket.media:
        return "media: yt-dlp owns extraction, fragments, and postprocessing"
    if bucket == WorkBucket.recursive_mirror:
        return "recursive mirror: bounded crawler queue with per-host politeness"
    if bucket in {WorkBucket.tiny, WorkBucket.small}:
        return "small direct file: queue slot preferred, no per-file splitting"
    if bucket in {WorkBucket.large, WorkBucket.huge} and item.supports_ranges:
        return "large ranged file: low queue concurrency with per-file segments"
    if bucket in {WorkBucket.large, WorkBucket.huge}:
        return "large file without ranges: low queue concurrency, single stream"
    if bucket == WorkBucket.unknown:
        return "unknown size: cautious queue until transfer reports a total"
    return f"{bucket.value}: {strategy}; backend {selected_backend}"


def _checksum_metadata_from_probe(probe: DirectFileProbe) -> dict[str, str]:
    return _checksum_metadata_from_headers(
        etag=probe.etag,
        last_modified=probe.last_modified,
    )


def _checksum_metadata_from_headers(
    *,
    etag: str | None,
    last_modified: str | None,
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if etag:
        metadata["etag"] = etag
    if last_modified:
        metadata["last_modified"] = last_modified
    return metadata


def _largest_size_class(items: list[WorkItem]) -> FileSizeClass:
    order = {
        FileSizeClass.unknown: 0,
        FileSizeClass.tiny: 1,
        FileSizeClass.small: 2,
        FileSizeClass.medium: 3,
        FileSizeClass.large: 4,
        FileSizeClass.huge: 5,
    }
    return max((item.size_class for item in items), key=lambda item: order[item])


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname


def _content_length(value: str | None) -> int | None:
    if value and value.isdigit():
        return int(value)
    return None


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
