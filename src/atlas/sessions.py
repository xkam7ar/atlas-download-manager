"""Shared smart download session builders."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from atlas.adaptive import work_item_from_probe
from atlas.models import (
    AdaptiveDownloadPlan,
    AdaptivePoliteness,
    AudioDownloadOptions,
    BatchKind,
    BatchSummary,
    DirectFileProbe,
    DirectoryMirrorOptions,
    DownloadPlan,
    FileDownloadOptions,
    FileSizeClass,
    HubKind,
    SiteDownloadOptions,
    SmartDownloadSession,
    VideoDownloadOptions,
    WorkBucket,
    WorkItem,
)


def media_session(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
    *,
    kind: HubKind,
) -> SmartDownloadSession:
    """Build the shared session envelope for video, audio, and media playlists."""

    is_playlist = not plan.noplaylist
    intent = "audio" if kind == HubKind.audio else "video"
    session_type = "media_playlist" if is_playlist else f"single_{intent}"
    queue = 2 if is_playlist else 1
    item = WorkItem(
        url=options.url,
        host=_host(options.url),
        kind=kind,
        bucket=WorkBucket.media,
        selected_backend="yt-dlp",
        priority=70,
        scheduler_decision=(
            "media playlist: adaptive yt-dlp queue with separate ffmpeg budget"
            if is_playlist
            else "single media job: yt-dlp owns extraction, fragments, and postprocessing"
        ),
        probed=False,
    )
    adaptive_plan = AdaptiveDownloadPlan(
        enabled=True,
        politeness=AdaptivePoliteness.normal,
        global_min_concurrency=1,
        global_max_concurrency=max(2, queue),
        queue_concurrency=queue,
        per_host_concurrency=queue,
        per_file_segments=1,
        per_file_segment_cap=1,
        max_active_files=queue,
        max_total_connections=queue,
        max_per_host_connections=queue,
        max_active_postprocessors=1,
        backend="yt-dlp",
        strategy=(
            "media playlist: adaptive download lane with bounded ffmpeg postprocessing"
            if is_playlist
            else "single media: one yt-dlp worker with phase-aware postprocessing"
        ),
        bucket_counts={WorkBucket.media.value: 1},
        hosts={item.host or "unknown": 1},
        work_items=[item],
        safety_notes=_media_safety_notes(options, plan),
    )
    return SmartDownloadSession(
        source=options.url,
        detected_kind=kind,
        intent=intent,
        session_type=session_type,
        manifest=[item],
        plan=adaptive_plan,
        customization=_media_customization(options, plan, intent=intent),
        scheduler_policy={
            "mode": "adaptive",
            "max_active_media": queue,
            "max_active_postprocessors": 1,
            "archive": options.archive,
            "playlist_explicit": is_playlist,
            "skip_download": plan.skip_download,
            "ignore_unavailable_playlist_entries": plan.ignore_unavailable_playlist_entries,
        },
        progress_reporter="media_rich",
        final_summary={"artifacts": ["saved paths", "archive state"]},
    )


def file_session(
    options: FileDownloadOptions,
    probe: DirectFileProbe,
    *,
    backend: str,
    backend_reason: str,
) -> SmartDownloadSession:
    """Build the shared session envelope for a direct-file download."""

    item = work_item_from_probe(probe)
    plan = options.adaptive_plan or _fixed_file_plan(item, backend=backend, options=options)
    return SmartDownloadSession(
        source=options.url,
        detected_kind=HubKind.file,
        intent="file",
        session_type="direct_file",
        manifest=plan.work_items or [item],
        plan=plan,
        customization={
            "backend": backend,
            "backend_reason": backend_reason,
            "resume": options.continue_download,
            "overwrite": options.overwrite,
            "checksum": bool(options.checksum),
            "connections": options.connections,
            "splits": options.splits,
        },
        scheduler_policy=_policy_from_plan(plan),
        progress_reporter="file_rich",
        final_summary={"artifacts": ["saved path", "checksum result"]},
    )


def site_session(
    options: SiteDownloadOptions,
    *,
    backend: str,
) -> SmartDownloadSession:
    """Build the shared session envelope for website and directory mirrors."""

    kind = HubKind.dir if isinstance(options, DirectoryMirrorOptions) else HubKind.site
    item = WorkItem(
        url=options.url,
        host=_host(options.url),
        kind=kind,
        bucket=WorkBucket.recursive_mirror,
        selected_backend=backend,
        priority=80,
        recursion_depth=0,
        scheduler_decision="recursive mirror preset: bounded by typed scope policy",
        probed=False,
    )
    plan = options.adaptive_plan or _fixed_site_plan(item, backend=backend, options=options)
    return SmartDownloadSession(
        source=options.url,
        detected_kind=kind,
        intent="directory_mirror" if kind == HubKind.dir else "site_mirror",
        session_type="directory_session" if kind == HubKind.dir else "site_session",
        manifest=plan.work_items or [item],
        plan=plan,
        customization={
            "depth": options.depth,
            "no_parent": options.no_parent,
            "domains": options.domains,
            "convert_links": options.convert_links,
            "keep_html": not _rejects_html(options.reject),
            "page_requisites": options.page_requisites,
            "resume": options.continue_download,
            "overwrite": options.overwrite,
            "wait": options.wait,
            "random_wait": options.random_wait,
            "timeout": options.timeout,
            "tries": options.tries,
            "max_files": options.max_files,
            "max_total_size": options.max_total_size,
            "max_runtime": options.max_runtime,
        },
        scheduler_policy=_policy_from_plan(plan),
        progress_reporter="mirror_rich",
        final_summary={"artifacts": ["stats", "failed URL samples"]},
    )


def batch_session(
    *,
    source: str,
    kind: BatchKind,
    output_dir: Path,
    adaptive_plan: AdaptiveDownloadPlan | None,
    total: int | None = None,
    summary: BatchSummary | None = None,
) -> SmartDownloadSession:
    """Build the shared session envelope for mixed batch queues."""

    detected = _batch_detected_kind(kind)
    plan = adaptive_plan or _fixed_batch_plan(detected, total=total)
    final_summary = (
        {
            "total": summary.total,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "skipped": summary.skipped,
        }
        if summary is not None
        else {"artifacts": ["summary json", "manifest json", "retry file"]}
    )
    return SmartDownloadSession(
        source=source,
        detected_kind=detected,
        intent=f"batch_{kind.value}",
        session_type="batch_session",
        manifest=plan.work_items,
        plan=plan,
        customization={
            "output_dir": str(output_dir),
            "kind": kind.value,
            "total": total,
        },
        scheduler_policy=_policy_from_plan(plan),
        progress_reporter="batch_rich",
        final_summary=final_summary,
    )


def _fixed_file_plan(
    item: WorkItem,
    *,
    backend: str,
    options: FileDownloadOptions,
) -> AdaptiveDownloadPlan:
    size_class = item.size_class
    bucket = item.bucket or WorkBucket(size_class.value)
    connections = max(1, options.connections if backend in {"aria2", "wget2"} else 1)
    return AdaptiveDownloadPlan(
        enabled=False,
        global_min_concurrency=1,
        global_max_concurrency=2,
        queue_concurrency=1,
        per_host_concurrency=1,
        per_file_segments=connections,
        per_file_segment_cap=connections,
        max_active_files=1,
        max_total_connections=connections,
        max_per_host_connections=connections,
        backend=backend,
        strategy="single direct-file preset",
        size_counts={size_class.value: 1},
        bucket_counts={bucket.value: 1},
        hosts={item.host or item.final_host or "unknown": 1},
        work_items=[item.model_copy(update={"bucket": bucket, "selected_backend": backend})],
        safety_notes=["smart session envelope; adaptive tuning disabled by request"],
    )


def _fixed_site_plan(
    item: WorkItem,
    *,
    backend: str,
    options: SiteDownloadOptions,
) -> AdaptiveDownloadPlan:
    connections = max(1, options.max_threads)
    return AdaptiveDownloadPlan(
        enabled=False,
        global_min_concurrency=1,
        global_max_concurrency=max(2, min(connections, 100)),
        queue_concurrency=1,
        per_host_concurrency=1,
        per_file_segments=1,
        per_file_segment_cap=1,
        max_active_files=1,
        max_total_connections=connections,
        max_per_host_connections=connections,
        backend=backend,
        strategy="bounded recursive mirror preset",
        bucket_counts={WorkBucket.recursive_mirror.value: 1},
        hosts={item.host or "unknown": 1},
        work_items=[item],
        safety_notes=["no-parent and host/domain policy remain explicit"],
    )


def _fixed_batch_plan(kind: HubKind, *, total: int | None) -> AdaptiveDownloadPlan:
    count = max(1, total or 1)
    return AdaptiveDownloadPlan(
        enabled=False,
        global_min_concurrency=1,
        global_max_concurrency=max(2, min(count, 100)),
        queue_concurrency=min(count, 2),
        per_host_concurrency=1,
        per_file_segments=1,
        per_file_segment_cap=1,
        max_active_files=min(count, 2),
        max_total_connections=min(count, 2),
        max_per_host_connections=1,
        backend="mixed",
        strategy="batch queue preset",
        bucket_counts={WorkBucket.unknown.value: count},
        work_items=[
            WorkItem(
                url="",
                kind=kind,
                size_class=FileSizeClass.unknown,
                bucket=WorkBucket.unknown,
                probed=False,
            )
        ],
        safety_notes=["batch owns queue concurrency; engines own per-item mechanics"],
    )


def _media_customization(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
    *,
    intent: str,
) -> dict[str, object]:
    data: dict[str, object] = {
        "mode": intent,
        "format": plan.format,
        "archive": options.archive,
        "metadata": options.embed_metadata,
        "thumbnail": options.embed_thumbnail,
        "subtitles": options.subtitle_mode.value,
        "skip_download": plan.skip_download,
        "sidecar_mode": _media_sidecar_mode(options),
        "playlist_items": options.playlist_items,
        "playlist_start": options.playlist_start,
        "playlist_end": options.playlist_end,
    }
    if isinstance(options, AudioDownloadOptions):
        data.update({"codec": options.codec.value, "quality": options.quality})
    else:
        data.update(
            {
                "quality": options.quality.value,
                "container": plan.merge_output_format,
                "resolution": options.resolution.value,
                "video_codec": options.video_codec.value,
                "hdr": options.hdr.value,
            }
        )
    return data


def _media_sidecar_mode(options: VideoDownloadOptions | AudioDownloadOptions) -> str | None:
    if options.subtitle_only:
        return "subtitle_only"
    if options.thumbnail_only:
        return "thumbnail_only"
    if options.info_only:
        return "info_only"
    if options.skip_download:
        return "skip_download"
    return None


def _media_safety_notes(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
) -> list[str]:
    notes = [
        "yt-dlp owns extractor/fragments/postprocessing",
        "media transfer and postprocessing phases remain separate",
    ]
    notes.append("playlist explicit" if not plan.noplaylist else "single item by default")
    notes.extend(plan.planner_notes)
    if plan.archive_file:
        notes.append("archive skip enabled")
    if options.browser_cookies or options.cookies_file:
        notes.append("cookies enabled")
    else:
        notes.append("cookies off")
    return notes


def _policy_from_plan(plan: AdaptiveDownloadPlan) -> dict[str, object]:
    return {
        "mode": "adaptive" if plan.enabled else "preset",
        "queue_concurrency": plan.queue_concurrency,
        "per_host_concurrency": plan.per_host_concurrency,
        "per_file_segments": plan.per_file_segments,
        "max_total_connections": plan.max_total_connections,
        "max_per_host_connections": plan.max_per_host_connections,
        "max_active_postprocessors": plan.max_active_postprocessors,
        "strategy": plan.strategy,
        "backend": plan.backend,
    }


def _batch_detected_kind(kind: BatchKind) -> HubKind:
    return {
        BatchKind.video: HubKind.video,
        BatchKind.audio: HubKind.audio,
        BatchKind.site: HubKind.site,
        BatchKind.dir: HubKind.dir,
        BatchKind.file: HubKind.file,
    }.get(kind, HubKind.auto)


def _rejects_html(reject: str | None) -> bool:
    values = {value.strip().lower().lstrip(".") for value in (reject or "").split(",")}
    return bool({"html", "htm"} & values)


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname
