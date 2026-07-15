"""Optimize hub requests into concrete engine options and preview plans."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from atlas.adaptive import (
    AdaptiveScheduler,
    default_adaptive_controls,
    plan_items_from_site_scan,
    scan_site,
    work_item_from_probe,
)
from atlas.backends import FileDownloadEngine, SiteMirrorEngine, filename_from_url
from atlas.config import AtlasSettings
from atlas.errors import AtlasError
from atlas.file_probe import probe_direct_file, unprobed_direct_file
from atlas.models import (
    AdaptiveDownloadPlan,
    AudioDownloadOptions,
    DirectFileProbe,
    DirectoryMirrorOptions,
    EngineKind,
    EngineRoute,
    FileBackendChoice,
    FileDownloadOptions,
    HubKind,
    HubRequest,
    OptimizedDownloadPlan,
    SiteBackendChoice,
    SiteDownloadOptions,
    VideoDownloadOptions,
)
from atlas.planner import SmartPlanner
from atlas.redaction import redact_command_args
from atlas.sessions import file_session, media_session, site_session

type DownloadOptions = (
    VideoDownloadOptions
    | AudioDownloadOptions
    | FileDownloadOptions
    | SiteDownloadOptions
    | DirectoryMirrorOptions
)

_SMALL_FILE_THRESHOLD_BYTES = 64 * 1024 * 1024
_SIZE_LIMIT_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>[KMGT]?)(?:B)?$")
_SIZE_LIMIT_UNITS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}
@dataclass(frozen=True)
class HubExecutionPlan:
    route: EngineRoute
    preview: OptimizedDownloadPlan
    options: DownloadOptions


class DownloadOptimizer:
    """Turn routed hub intent into safe, concrete engine options."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def optimize(
        self,
        request: HubRequest,
        route: EngineRoute,
        *,
        backend: str = "auto",
        checksum: str | None = None,
    ) -> HubExecutionPlan:
        if route.kind == HubKind.audio:
            audio_options = self._audio_options(request)
            return self.optimize_options(route, audio_options)

        if route.kind == HubKind.video:
            video_options = self._video_options(request)
            return self.optimize_options(route, video_options)

        if route.kind == HubKind.site:
            site_options = self._site_options(request, backend)
            return self.optimize_options(route, site_options)

        if route.kind == HubKind.dir:
            dir_options = self._dir_options(request, backend)
            return self.optimize_options(route, dir_options)

        if route.kind == HubKind.manifest:
            file_options = self._file_options(
                request,
                FileBackendChoice.aria2.value,
                checksum=checksum,
                force_metalink=True,
            )
            return self.optimize_options(route, file_options)

        file_options = self._file_options(request, backend, checksum=checksum)
        return self.optimize_options(route, file_options)

    def optimize_options(
        self,
        route: EngineRoute,
        options: DownloadOptions,
    ) -> HubExecutionPlan:
        """Build an explainable hub execution plan from already-typed options."""

        if isinstance(options, AudioDownloadOptions):
            media_plan = SmartPlanner(self._settings).plan_audio(options)
            session = media_session(options, media_plan, kind=HubKind.audio)
            preview = OptimizedDownloadPlan(
                route=route,
                output=options.output_dir,
                session=session,
                summary={
                    "quality": "audio",
                    "format": media_plan.format,
                    "codec": options.codec.value,
                    "audio_quality": options.quality,
                    "noplaylist": media_plan.noplaylist,
                    "skip_download": media_plan.skip_download,
                    "ignore_unavailable_playlist_entries": (
                        media_plan.ignore_unavailable_playlist_entries
                    ),
                    "planner_notes": media_plan.planner_notes,
                    "archive": options.archive,
                    "metadata": options.embed_metadata,
                    "thumbnail": options.embed_thumbnail,
                },
            )
            return HubExecutionPlan(route=route, preview=preview, options=options)

        if isinstance(options, VideoDownloadOptions):
            media_plan = SmartPlanner(self._settings).plan_video(options)
            session = media_session(options, media_plan, kind=HubKind.video)
            preview = OptimizedDownloadPlan(
                route=route,
                output=options.output_dir,
                session=session,
                summary={
                    "quality": options.quality.value,
                    "format": media_plan.format,
                    "container": media_plan.merge_output_format,
                    "video_codec": options.video_codec.value,
                    "noplaylist": media_plan.noplaylist,
                    "skip_download": media_plan.skip_download,
                    "ignore_unavailable_playlist_entries": (
                        media_plan.ignore_unavailable_playlist_entries
                    ),
                    "planner_notes": media_plan.planner_notes,
                    "archive": options.archive,
                    "metadata": options.embed_metadata,
                    "thumbnail": options.embed_thumbnail,
                    "playlist": options.playlist,
                },
            )
            return HubExecutionPlan(route=route, preview=preview, options=options)

        if isinstance(options, SiteDownloadOptions):
            optimized_site_options = self._optimize_site_options(options)
            backend_plan = SiteMirrorEngine().plan(optimized_site_options)
            optimized_route = route.model_copy(
                update={"engine": _engine_kind_for_site_backend(backend_plan.backend)}
            )
            mirror_kind = (
                "dir" if isinstance(optimized_site_options, DirectoryMirrorOptions) else "site"
            )
            preview = OptimizedDownloadPlan(
                route=optimized_route,
                output=backend_plan.output,
                args=backend_plan.args,
                session=site_session(
                    optimized_site_options,
                    backend=backend_plan.backend,
                ),
                summary={
                    "mirror_kind": mirror_kind,
                    "backend": backend_plan.backend,
                    "depth": optimized_site_options.depth,
                    "assets": optimized_site_options.page_requisites,
                    "convert_links": optimized_site_options.convert_links,
                    "span_hosts": optimized_site_options.span_hosts,
                    "robots": optimized_site_options.robots,
                    "follow_sitemaps": optimized_site_options.follow_sitemaps,
                    "no_parent": optimized_site_options.no_parent,
                    "domains": optimized_site_options.domains,
                    "exclude_domains": optimized_site_options.exclude_domains,
                    "include_directories": optimized_site_options.include_directories,
                    "exclude_directories": optimized_site_options.exclude_directories,
                    "accept_regex": optimized_site_options.accept_regex,
                    "reject_regex": optimized_site_options.reject_regex,
                    "filter_mime_type": optimized_site_options.filter_mime_type,
                    "filter_urls": optimized_site_options.filter_urls,
                    "ignore_case": optimized_site_options.ignore_case,
                    "follow_tags": optimized_site_options.follow_tags,
                    "ignore_tags": optimized_site_options.ignore_tags,
                    "directories": optimized_site_options.directories,
                    "host_directories": optimized_site_options.host_directories,
                    "protocol_directories": optimized_site_options.protocol_directories,
                    "cut_dirs": optimized_site_options.cut_dirs,
                    "default_page": optimized_site_options.default_page,
                    "adjust_extension": optimized_site_options.adjust_extension,
                    "convert_file_only": optimized_site_options.convert_file_only,
                    "cut_url_get_vars": optimized_site_options.cut_url_get_vars,
                    "cut_file_get_vars": optimized_site_options.cut_file_get_vars,
                    "keep_extension": optimized_site_options.keep_extension,
                    "unlink": optimized_site_options.unlink,
                    "input_file": optimized_site_options.input_file,
                    "input_file_only": optimized_site_options.input_file_only,
                    "base": optimized_site_options.base,
                    "force_metalink": optimized_site_options.force_metalink,
                    "warc_file": optimized_site_options.warc_file,
                    "warc_compression": optimized_site_options.warc_compression,
                    "warc_cdx": optimized_site_options.warc_cdx,
                    "warc_max_size": optimized_site_options.warc_max_size,
                    "user_agent": optimized_site_options.user_agent,
                    "headers": len(optimized_site_options.headers),
                    "referer": bool(optimized_site_options.referer),
                    "cache": optimized_site_options.cache,
                    "compression": optimized_site_options.compression,
                    "method": optimized_site_options.method,
                    "cookies": bool(
                        optimized_site_options.cookies is not None
                        or optimized_site_options.browser_cookies
                        or optimized_site_options.load_cookies
                        or optimized_site_options.save_cookies
                    ),
                    "browser_cookies": bool(optimized_site_options.browser_cookies),
                    "https_only": optimized_site_options.https_only,
                    "https_enforce": optimized_site_options.https_enforce,
                    "hsts": optimized_site_options.hsts,
                    "hsts_file": optimized_site_options.hsts_file,
                    "check_certificate": optimized_site_options.check_certificate,
                    "check_hostname": optimized_site_options.check_hostname,
                    "certificate_type": optimized_site_options.certificate_type,
                    "private_key_type": optimized_site_options.private_key_type,
                    "crl_file": optimized_site_options.crl_file,
                    "ocsp": optimized_site_options.ocsp,
                    "ocsp_file": optimized_site_options.ocsp_file,
                    "ocsp_stapling": optimized_site_options.ocsp_stapling,
                    "tls_resume": optimized_site_options.tls_resume,
                    "tls_session_file": optimized_site_options.tls_session_file,
                    "tls_false_start": optimized_site_options.tls_false_start,
                    "http2": optimized_site_options.http2,
                    "http2_only": optimized_site_options.http2_only,
                    "content_on_error": optimized_site_options.content_on_error,
                    "save_content_on": optimized_site_options.save_content_on,
                    "save_headers": optimized_site_options.save_headers,
                    "server_response": optimized_site_options.server_response,
                    "ignore_length": optimized_site_options.ignore_length,
                    "verify_sig": optimized_site_options.verify_sig,
                    "signature_extensions": optimized_site_options.signature_extensions,
                    "gnupg_homedir": optimized_site_options.gnupg_homedir,
                    "verify_save_failed": optimized_site_options.verify_save_failed,
                    "max_files": optimized_site_options.max_files,
                    "max_total_size": optimized_site_options.max_total_size,
                    "max_runtime": optimized_site_options.max_runtime,
                    "quota": optimized_site_options.quota,
                    "limit_rate": optimized_site_options.limit_rate,
                    "retry_connrefused": optimized_site_options.retry_connrefused,
                    "inet4_only": optimized_site_options.inet4_only,
                    "inet6_only": optimized_site_options.inet6_only,
                    "bind_address": optimized_site_options.bind_address,
                    "bind_interface": optimized_site_options.bind_interface,
                    "prefer_family": optimized_site_options.prefer_family,
                    "dns_cache": optimized_site_options.dns_cache,
                    "dns_cache_preload": optimized_site_options.dns_cache_preload,
                    "tcp_fastopen": optimized_site_options.tcp_fastopen,
                    "max_threads": optimized_site_options.max_threads,
                    "tries": optimized_site_options.tries,
                    "waitretry": optimized_site_options.waitretry,
                    "retry_on_http_error": optimized_site_options.retry_on_http_error,
                    "max_redirect": optimized_site_options.max_redirect,
                    "timeout": optimized_site_options.timeout,
                    "dns_timeout": optimized_site_options.dns_timeout,
                    "connect_timeout": optimized_site_options.connect_timeout,
                    "read_timeout": optimized_site_options.read_timeout,
                    "random_wait": optimized_site_options.random_wait,
                    "timestamping": optimized_site_options.timestamping,
                    "if_modified_since": optimized_site_options.if_modified_since,
                    "resume": optimized_site_options.continue_download,
                    "overwrite": optimized_site_options.overwrite,
                    "spider": optimized_site_options.spider,
                    "wait": optimized_site_options.wait,
                    "warnings": backend_plan.warnings,
                    "adaptive": _adaptive_summary(optimized_site_options.adaptive_plan),
                },
            )
            return HubExecutionPlan(
                route=optimized_route,
                preview=preview,
                options=optimized_site_options,
            )

        assert isinstance(options, FileDownloadOptions)
        optimized_file_options, probe, backend_reason = self._optimize_file_options(options)
        backend_plan = FileDownloadEngine().plan(optimized_file_options)
        optimized_route = route.model_copy(
            update={"engine": _engine_kind_for_file_backend(backend_plan.backend)}
        )
        preview = OptimizedDownloadPlan(
            route=optimized_route,
            output=backend_plan.output,
            args=backend_plan.args,
            session=file_session(
                optimized_file_options,
                probe,
                backend=backend_plan.backend,
                backend_reason=backend_reason,
            ),
            summary={
                "backend": backend_plan.backend,
                "backend_reason": backend_reason,
                "resume": optimized_file_options.continue_download,
                "overwrite": optimized_file_options.overwrite,
                "trust_server_names": optimized_file_options.trust_server_names,
                "content_disposition": optimized_file_options.content_disposition,
                "timestamping": optimized_file_options.timestamping,
                "use_server_timestamps": optimized_file_options.use_server_timestamps,
                "metalink": optimized_file_options.metalink,
                "force_metalink": optimized_file_options.force_metalink,
                "user_agent": optimized_file_options.user_agent,
                "headers": len(optimized_file_options.headers),
                "referer": bool(optimized_file_options.referer),
                "cache": optimized_file_options.cache,
                "compression": optimized_file_options.compression,
                "method": optimized_file_options.method,
                "connections": optimized_file_options.connections,
                "splits": optimized_file_options.splits,
                "chunk_size": optimized_file_options.chunk_size,
                "lowest_speed_limit": optimized_file_options.lowest_speed_limit,
                "max_tries": optimized_file_options.max_tries,
                "retry_wait": optimized_file_options.retry_wait,
                "timeout": optimized_file_options.timeout,
                "connect_timeout": optimized_file_options.connect_timeout,
                "file_allocation": optimized_file_options.file_allocation,
                "check_integrity": optimized_file_options.check_integrity,
                "remote_time": optimized_file_options.remote_time,
                "conditional_get": optimized_file_options.conditional_get,
                "http_accept_gzip": optimized_file_options.http_accept_gzip,
                "input_file": (
                    str(optimized_file_options.input_file)
                    if optimized_file_options.input_file
                    else None
                ),
                "save_session": (
                    str(optimized_file_options.save_session)
                    if optimized_file_options.save_session
                    else None
                ),
                "save_session_interval": optimized_file_options.save_session_interval,
                "metalink_preferred_protocol": (
                    optimized_file_options.metalink_preferred_protocol.value
                    if optimized_file_options.metalink_preferred_protocol
                    else None
                ),
                "metalink_language": optimized_file_options.metalink_language,
                "metalink_os": optimized_file_options.metalink_os,
                "metalink_location": optimized_file_options.metalink_location,
                "metalink_base_uri": optimized_file_options.metalink_base_uri,
                "metalink_enable_unique_protocol": (
                    optimized_file_options.metalink_enable_unique_protocol
                ),
                "server_stat_if": (
                    str(optimized_file_options.server_stat_if)
                    if optimized_file_options.server_stat_if
                    else None
                ),
                "server_stat_of": (
                    str(optimized_file_options.server_stat_of)
                    if optimized_file_options.server_stat_of
                    else None
                ),
                "server_stat_timeout": optimized_file_options.server_stat_timeout,
                "uri_selector": (
                    optimized_file_options.uri_selector.value
                    if optimized_file_options.uri_selector
                    else None
                ),
                "checksum": optimized_file_options.checksum,
                "probe": _probe_summary(probe),
                "adaptive": _adaptive_summary(optimized_file_options.adaptive_plan),
            },
        )
        return HubExecutionPlan(
            route=optimized_route,
            preview=preview,
            options=optimized_file_options,
        )

    def _video_options(self, request: HubRequest) -> VideoDownloadOptions:
        return VideoDownloadOptions(
            url=request.url,
            output_dir=request.output_dir,
            archive=self._settings.archive,
            archive_file=self._settings.archive_file,
            use_aria2=self._settings.aria2,
            concurrent_fragments=self._settings.media_concurrent_fragments,
            file_access_retries=self._settings.media_file_access_retries,
            retry_sleep=self._settings.media_retry_sleep,
            skip_unavailable_fragments=self._settings.media_skip_unavailable_fragments,
            throttled_rate=self._settings.media_throttled_rate,
            http_chunk_size=self._settings.media_http_chunk_size,
            socket_timeout=self._settings.media_socket_timeout,
            source_address=self._settings.media_source_address,
            impersonate=self._settings.media_impersonate,
            extractor_args=self._settings.media_extractor_args,
            match_filters=self._settings.media_match_filters,
            break_match_filters=self._settings.media_break_match_filters,
            max_downloads=self._settings.media_max_downloads,
            break_on_existing=self._settings.media_break_on_existing,
            break_on_reject=self._settings.media_break_on_reject,
            break_per_input=self._settings.media_break_per_input,
            date=self._settings.media_date,
            date_before=self._settings.media_date_before,
            date_after=self._settings.media_date_after,
            min_filesize=self._settings.media_min_filesize,
            max_filesize=self._settings.media_max_filesize,
            reject_live=self._settings.media_reject_live,
            reject_upcoming=self._settings.media_reject_upcoming,
            live_from_start=self._settings.media_live_from_start,
            download_sections=self._settings.media_download_sections,
            sponsorblock_mark=self._settings.media_sponsorblock_mark,
            sponsorblock_remove=self._settings.media_sponsorblock_remove,
            sponsorblock_chapter_title=self._settings.media_sponsorblock_chapter_title,
            sponsorblock_api=self._settings.media_sponsorblock_api,
            write_info_json=self._settings.write_info_json,
            write_thumbnail=self._settings.write_thumbnail,
            embed_thumbnail=self._settings.embed_thumbnail,
            embed_metadata=self._settings.embed_metadata,
            dry_run=request.dry_run,
            quiet=request.quiet,
            json_output=request.json_output,
            progress_mode=request.progress_mode,
            verbose=request.verbose,
            container=self._settings.video_container,
            video_codec=request.video_codec,
        )

    def _audio_options(self, request: HubRequest) -> AudioDownloadOptions:
        return AudioDownloadOptions(
            url=request.url,
            output_dir=request.output_dir,
            archive=self._settings.archive,
            archive_file=self._settings.archive_file,
            use_aria2=self._settings.aria2,
            concurrent_fragments=self._settings.media_concurrent_fragments,
            file_access_retries=self._settings.media_file_access_retries,
            retry_sleep=self._settings.media_retry_sleep,
            skip_unavailable_fragments=self._settings.media_skip_unavailable_fragments,
            throttled_rate=self._settings.media_throttled_rate,
            http_chunk_size=self._settings.media_http_chunk_size,
            socket_timeout=self._settings.media_socket_timeout,
            source_address=self._settings.media_source_address,
            impersonate=self._settings.media_impersonate,
            extractor_args=self._settings.media_extractor_args,
            match_filters=self._settings.media_match_filters,
            break_match_filters=self._settings.media_break_match_filters,
            max_downloads=self._settings.media_max_downloads,
            break_on_existing=self._settings.media_break_on_existing,
            break_on_reject=self._settings.media_break_on_reject,
            break_per_input=self._settings.media_break_per_input,
            date=self._settings.media_date,
            date_before=self._settings.media_date_before,
            date_after=self._settings.media_date_after,
            min_filesize=self._settings.media_min_filesize,
            max_filesize=self._settings.media_max_filesize,
            reject_live=self._settings.media_reject_live,
            reject_upcoming=self._settings.media_reject_upcoming,
            live_from_start=self._settings.media_live_from_start,
            download_sections=self._settings.media_download_sections,
            sponsorblock_mark=self._settings.media_sponsorblock_mark,
            sponsorblock_remove=self._settings.media_sponsorblock_remove,
            sponsorblock_chapter_title=self._settings.media_sponsorblock_chapter_title,
            sponsorblock_api=self._settings.media_sponsorblock_api,
            write_info_json=self._settings.write_info_json,
            write_thumbnail=self._settings.write_thumbnail,
            embed_thumbnail=self._settings.embed_thumbnail,
            embed_metadata=self._settings.embed_metadata,
            dry_run=request.dry_run,
            quiet=request.quiet,
            json_output=request.json_output,
            progress_mode=request.progress_mode,
            verbose=request.verbose,
            codec=request.audio_codec or self._settings.audio_codec,
            quality=(
                self._settings.audio_quality
                if request.audio_quality is None
                else request.audio_quality
            ),
        )

    def _file_options(
        self,
        request: HubRequest,
        backend: str,
        *,
        checksum: str | None,
        force_metalink: bool = False,
    ) -> FileDownloadOptions:
        return FileDownloadOptions(
            url=request.url,
            output_dir=request.output_dir,
            backend=_file_backend(self._settings, backend),
            connections=self._settings.aria2_connections,
            splits=self._settings.aria2_splits,
            chunk_size=self._settings.aria2_chunk_size,
            checksum=checksum,
            trust_server_names=self._settings.file_trust_server_names,
            content_disposition=self._settings.file_content_disposition,
            timestamping=self._settings.file_timestamping,
            use_server_timestamps=self._settings.file_use_server_timestamps,
            timeout=self._settings.file_timeout,
            lowest_speed_limit=self._settings.file_lowest_speed_limit,
            max_tries=self._settings.file_max_tries,
            retry_wait=self._settings.file_retry_wait,
            connect_timeout=self._settings.file_connect_timeout,
            file_allocation=self._settings.file_file_allocation,
            check_integrity=self._settings.file_check_integrity,
            remote_time=self._settings.file_remote_time,
            conditional_get=self._settings.file_conditional_get,
            http_accept_gzip=self._settings.file_http_accept_gzip,
            input_file=self._settings.file_input_file,
            save_session=self._settings.file_save_session,
            save_session_interval=self._settings.file_save_session_interval,
            metalink_preferred_protocol=self._settings.file_metalink_preferred_protocol,
            metalink_language=self._settings.file_metalink_language,
            metalink_os=self._settings.file_metalink_os,
            metalink_location=self._settings.file_metalink_location,
            metalink_base_uri=self._settings.file_metalink_base_uri,
            metalink_enable_unique_protocol=self._settings.file_metalink_enable_unique_protocol,
            server_stat_if=self._settings.file_server_stat_if,
            server_stat_of=self._settings.file_server_stat_of,
            server_stat_timeout=self._settings.file_server_stat_timeout,
            uri_selector=self._settings.file_uri_selector,
            force_metalink=force_metalink,
            dry_run=request.dry_run,
            adaptive=request.adaptive,
            max_concurrency=request.max_concurrency,
            per_host_concurrency=request.per_host_concurrency,
            politeness=request.politeness,
            explain=request.explain,
            quiet=request.quiet,
            json_output=request.json_output,
            progress_mode=request.progress_mode,
            verbose=request.verbose,
        )

    def _optimize_file_options(
        self,
        options: FileDownloadOptions,
    ) -> tuple[FileDownloadOptions, DirectFileProbe, str]:
        if options.force_metalink:
            probe = unprobed_direct_file(options.url, reason="metalink manifest")
        elif options.dry_run:
            probe = unprobed_direct_file(options.url, reason="dry run: probe skipped")
        else:
            probe = probe_direct_file(options.url)

        metalink_url = (
            probe.metalink_url if options.metalink and not options.force_metalink else None
        )
        adaptive_plan = self._adaptive_file_plan(options, probe)
        selected_backend, reason = _select_file_backend(
            self._settings,
            options.backend,
            probe,
        )
        upgrade_to_metalink = metalink_url is not None and options.backend in {
            FileBackendChoice.auto,
            FileBackendChoice.aria2,
        }
        if upgrade_to_metalink:
            selected_backend = FileBackendChoice.aria2
            reason = f"HTTP Link rel={probe.metalink_source or 'describedby'} Metalink"
        adaptive_reason = None
        if not upgrade_to_metalink:
            selected_backend, adaptive_reason = _apply_adaptive_backend(
                self._settings,
                options.backend,
                selected_backend,
                adaptive_plan,
            )
        if adaptive_reason:
            reason = adaptive_reason
        filename = _file_output_name(options, probe)
        updates: dict[str, object] = {
            "backend": selected_backend,
            "filename": filename,
            "probe": probe,
            "adaptive_plan": adaptive_plan,
        }
        if adaptive_plan:
            updates["connections"] = adaptive_plan.per_file_segments
            updates["splits"] = adaptive_plan.per_file_segments
            if adaptive_plan.speed_limit and not options.rate_limit:
                updates["rate_limit"] = adaptive_plan.speed_limit
        if upgrade_to_metalink:
            updates["url"] = metalink_url
            updates["filename"] = options.filename
            updates["force_metalink"] = True
        return (
            options.model_copy(update=updates),
            probe,
            reason,
        )

    def _adaptive_file_plan(
        self,
        options: FileDownloadOptions,
        probe: DirectFileProbe,
    ) -> AdaptiveDownloadPlan | None:
        if not (options.adaptive or options.explain):
            return None
        controls = default_adaptive_controls(
            enabled=True,
            max_concurrency=options.max_concurrency,
            per_host_concurrency=options.per_host_concurrency,
            politeness=options.politeness,
            dry_run=options.dry_run,
        )
        item = work_item_from_probe(probe)
        return AdaptiveScheduler(
            max_concurrency=controls.max_concurrency,
            per_host_concurrency=controls.per_host_concurrency,
            politeness=controls.politeness,
        ).plan([item], kind=HubKind.file, backend=options.backend.value)

    def _site_options(self, request: HubRequest, backend: str) -> SiteDownloadOptions:
        return SiteDownloadOptions(
            url=request.url,
            output_dir=request.output_dir,
            backend=_site_backend(self._settings, backend),
            depth=self._settings.site_depth,
            page_requisites=self._settings.site_page_requisites,
            convert_links=self._settings.site_convert_links,
            span_hosts=self._settings.site_span_hosts,
            wait=self._settings.site_wait,
            accept=self._settings.site_accept,
            reject=self._settings.site_reject,
            robots=self._settings.site_robots,
            follow_sitemaps=self._settings.site_follow_sitemaps,
            no_parent=self._settings.site_no_parent,
            domains=self._settings.site_domains,
            exclude_domains=self._settings.site_exclude_domains,
            include_directories=self._settings.site_include_directories,
            exclude_directories=self._settings.site_exclude_directories,
            accept_regex=self._settings.site_accept_regex,
            reject_regex=self._settings.site_reject_regex,
            filter_mime_type=self._settings.site_filter_mime_type,
            ignore_case=self._settings.site_ignore_case,
            max_files=self._settings.site_max_files,
            max_total_size=self._settings.site_max_total_size,
            max_runtime=self._settings.site_max_runtime,
            max_threads=self._settings.site_max_threads,
            tries=self._settings.site_tries,
            waitretry=self._settings.site_waitretry,
            retry_on_http_error=self._settings.site_retry_on_http_error,
            max_redirect=self._settings.site_max_redirect,
            timeout=self._settings.site_timeout,
            dns_timeout=self._settings.site_dns_timeout,
            connect_timeout=self._settings.site_connect_timeout,
            read_timeout=self._settings.site_read_timeout,
            random_wait=self._settings.site_random_wait,
            timestamping=self._settings.site_timestamping,
            stats=self._settings.site_stats,
            dry_run=request.dry_run,
            adaptive=request.adaptive,
            max_concurrency=request.max_concurrency,
            per_host_concurrency=request.per_host_concurrency,
            politeness=request.politeness,
            explain=request.explain,
            quiet=request.quiet,
            json_output=request.json_output,
            progress_mode=request.progress_mode,
            verbose=request.verbose,
        )

    def _dir_options(self, request: HubRequest, backend: str) -> DirectoryMirrorOptions:
        return DirectoryMirrorOptions(
            url=request.url,
            output_dir=request.output_dir,
            backend=_dir_backend(self._settings, backend),
            depth=self._settings.dir_depth,
            wait=self._settings.dir_wait,
            accept=self._settings.site_accept,
            reject=self._settings.site_reject,
            robots=self._settings.site_robots,
            follow_sitemaps=False,
            no_parent=True,
            max_files=self._settings.site_max_files,
            max_total_size=self._settings.site_max_total_size,
            max_runtime=self._settings.site_max_runtime,
            max_threads=self._settings.site_max_threads,
            tries=self._settings.site_tries,
            waitretry=self._settings.site_waitretry,
            retry_on_http_error=self._settings.site_retry_on_http_error,
            max_redirect=self._settings.site_max_redirect,
            timeout=self._settings.site_timeout,
            dns_timeout=self._settings.site_dns_timeout,
            connect_timeout=self._settings.site_connect_timeout,
            read_timeout=self._settings.site_read_timeout,
            random_wait=self._settings.site_random_wait,
            timestamping=self._settings.dir_timestamping,
            user_agent=self._settings.dir_user_agent,
            if_modified_since=self._settings.dir_if_modified_since,
            stats=self._settings.site_stats,
            dry_run=request.dry_run,
            adaptive=request.adaptive,
            max_concurrency=request.max_concurrency,
            per_host_concurrency=request.per_host_concurrency,
            politeness=request.politeness,
            explain=request.explain,
            quiet=request.quiet,
            json_output=request.json_output,
            progress_mode=request.progress_mode,
            verbose=request.verbose,
        )

    def _optimize_site_options(self, options: SiteDownloadOptions) -> SiteDownloadOptions:
        if not (options.adaptive or options.explain):
            return options
        controls = default_adaptive_controls(
            enabled=True,
            max_concurrency=options.max_concurrency,
            per_host_concurrency=options.per_host_concurrency,
            politeness=options.politeness,
            dry_run=options.dry_run,
        )
        item = scan_site(options.url, dry_run=options.dry_run)
        _enforce_mirror_scan_limits(options, item)
        plan_kind = HubKind.dir if isinstance(options, DirectoryMirrorOptions) else HubKind.site
        if plan_kind == HubKind.dir:
            item = item.model_copy(update={"kind": HubKind.dir})
        plan_items = plan_items_from_site_scan(item, kind=plan_kind)
        adaptive_plan = AdaptiveScheduler(
            max_concurrency=controls.max_concurrency,
            per_host_concurrency=controls.per_host_concurrency,
            politeness=controls.politeness,
        ).plan(plan_items, kind=plan_kind, backend=options.backend.value)
        wait_floor = {
            "normal": 1.0,
            "fast": 0.25,
            "aggressive": 0.0,
        }[options.politeness.value]
        wait = max(options.wait or 0.0, wait_floor)
        return options.model_copy(update={"wait": wait, "adaptive_plan": adaptive_plan})


def plan_as_dict(plan: OptimizedDownloadPlan) -> dict[str, object]:
    """Return a compact JSON-friendly plan preview."""

    data = plan.model_dump(mode="json")
    output = data.get("output")
    if isinstance(output, Path):
        data["output"] = str(output)
    args = data.get("args")
    if isinstance(args, list):
        data["args"] = _redact_backend_args(args)
    return data


def _redact_backend_args(args: list[object]) -> list[object]:
    return redact_command_args(args)


def _enforce_mirror_scan_limits(options: SiteDownloadOptions, item: object) -> None:
    counts = getattr(item, "scan_counts", {}) or {}
    discovered = int(counts.get("same_host") or 0)
    if options.max_files is not None and discovered > options.max_files:
        raise AtlasError(
            f"Mirror scan found {discovered} same-host items, exceeding --max-files "
            f"{options.max_files}. Narrow depth/scope or raise the limit."
        )

    estimated = getattr(item, "scan_estimated_bytes", None)
    limit = _parse_size_limit(options.max_total_size or options.quota)
    if estimated is not None and limit is not None and estimated > limit:
        raise AtlasError(
            f"Mirror scan estimated {estimated} bytes, exceeding --max-total-size "
            f"{options.max_total_size or options.quota}. Narrow scope or raise the limit."
        )


def _parse_size_limit(value: str | None) -> int | None:
    if value is None:
        return None
    match = _SIZE_LIMIT_PATTERN.fullmatch(value.strip().upper())
    if match is None:
        return None
    number = float(match.group("number"))
    unit = match.group("unit")
    return int(number * _SIZE_LIMIT_UNITS[unit])


def _file_backend(settings: AtlasSettings, backend: str) -> FileBackendChoice:
    if backend == "auto":
        return settings.file_backend
    try:
        return FileBackendChoice(backend)
    except ValueError as exc:
        msg = "--backend for file downloads must be auto, native, aria2, or wget2"
        raise AtlasError(msg) from exc


def _file_output_name(options: FileDownloadOptions, probe: DirectFileProbe) -> str | None:
    if options.filename:
        return options.filename
    if probe.probed and options.content_disposition and probe.filename:
        return probe.filename
    if probe.probed and options.trust_server_names and probe.final_url:
        return filename_from_url(probe.final_url)
    return None


def _select_file_backend(
    settings: AtlasSettings,
    selected: FileBackendChoice,
    probe: DirectFileProbe,
) -> tuple[FileBackendChoice, str]:
    if selected != FileBackendChoice.auto:
        return selected, f"user selected {selected.value}"
    if not settings.aria2:
        return FileBackendChoice.native, "aria2 disabled in config"
    if not shutil.which("aria2c"):
        return FileBackendChoice.native, "aria2c not installed"
    if not probe.probed:
        return FileBackendChoice.aria2, "unknown size; aria2 enabled"
    if probe.content_length is None:
        return FileBackendChoice.aria2, "unknown size; aria2 enabled"
    if probe.content_length < _SMALL_FILE_THRESHOLD_BYTES:
        return FileBackendChoice.native, "small file"
    if probe.supports_ranges:
        return FileBackendChoice.aria2, "large file with range support"
    return FileBackendChoice.native, "large file without range support"


def _apply_adaptive_backend(
    settings: AtlasSettings,
    requested: FileBackendChoice,
    selected: FileBackendChoice,
    adaptive_plan: AdaptiveDownloadPlan | None,
) -> tuple[FileBackendChoice, str | None]:
    if adaptive_plan is None or requested != FileBackendChoice.auto:
        return selected, None
    if adaptive_plan.backend == FileBackendChoice.native.value:
        return FileBackendChoice.native, adaptive_plan.strategy
    if adaptive_plan.backend == FileBackendChoice.aria2.value:
        if settings.aria2 and shutil.which("aria2c"):
            return FileBackendChoice.aria2, adaptive_plan.strategy
        return FileBackendChoice.native, "adaptive wanted ranged segments but aria2c is unavailable"
    if adaptive_plan.per_file_segments <= 1 and "unknown sizes" in adaptive_plan.strategy:
        return FileBackendChoice.native, adaptive_plan.strategy
    return selected, None


def _probe_summary(probe: DirectFileProbe) -> dict[str, object]:
    summary = probe.model_dump(mode="json", exclude_none=True)
    if probe.error:
        summary.setdefault("reason", probe.error)
    return summary


def _adaptive_summary(plan: AdaptiveDownloadPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return plan.model_dump(mode="json", exclude_none=True)


def _engine_kind_for_file_backend(backend: str) -> EngineKind:
    if backend == FileBackendChoice.aria2.value:
        return EngineKind.aria2
    if backend == FileBackendChoice.native.value:
        return EngineKind.native
    if backend == FileBackendChoice.wget2.value:
        return EngineKind.wget2
    return EngineKind.unknown


def _engine_kind_for_site_backend(backend: str) -> EngineKind:
    if backend == SiteBackendChoice.wget.value:
        return EngineKind.wget
    if backend == SiteBackendChoice.wget2.value:
        return EngineKind.wget2
    return EngineKind.unknown


def _site_backend(settings: AtlasSettings, backend: str) -> SiteBackendChoice:
    if backend == "auto":
        return settings.site_backend
    try:
        return SiteBackendChoice(backend)
    except ValueError as exc:
        msg = "--backend for site mirrors must be auto, wget2, or wget"
        raise AtlasError(msg) from exc


def _dir_backend(settings: AtlasSettings, backend: str) -> SiteBackendChoice:
    if backend == "auto":
        return settings.dir_backend
    try:
        return SiteBackendChoice(backend)
    except ValueError as exc:
        msg = "--backend for directory mirrors must be auto, wget2, or wget"
        raise AtlasError(msg) from exc
