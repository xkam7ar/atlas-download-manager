"""External and native downloader backends for non-media hub commands."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from atlas.aria2_rpc import Aria2RpcSession, Aria2RpcStartupError
from atlas.errors import DependencyMissingError, EngineError
from atlas.models import (
    DirectoryMirrorOptions,
    DownloadResult,
    DownloadStatus,
    EngineKind,
    FileBackendChoice,
    FileDownloadOptions,
    HubKind,
    ProgressEvent,
    ProgressPhase,
    SiteBackendChoice,
    SiteDownloadOptions,
)
from atlas.paths import safe_filename
from atlas.private_files import replace_private_text
from atlas.progress_events import progress_event_from_aria2_line, progress_event_from_wget2_line
from atlas.redaction import redact_url
from atlas.runner import ProcessCanceled, ProcessControl, run_args_stream
from atlas.urls import is_metalink_url

ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class BackendPlan:
    backend: str
    args: list[str]
    output: Path
    stats_files: dict[str, Path] | None = None
    warnings: list[str] = field(default_factory=list)


class FileDownloadEngine:
    """Download direct files through native Python, aria2c, or wget2."""

    def plan(self, options: FileDownloadOptions) -> BackendPlan:
        backend = self._resolve_backend(options, dry_run=options.dry_run)
        output = _safe_output_path(
            options.output_dir,
            options.filename or filename_from_url(options.url),
        )
        args = (
            self._aria2_rpc_args_preview(options)
            if backend == FileBackendChoice.aria2
            else (
                self._wget2_file_args(options, output)
                if backend == FileBackendChoice.wget2
                else ["native", options.url, "--output", str(output)]
            )
        )
        return BackendPlan(backend=backend.value, args=args, output=output)

    def download(
        self,
        options: FileDownloadOptions,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        plan = self.plan(options)
        if options.dry_run:
            return DownloadResult(
                status=DownloadStatus.dry_run,
                url=options.url,
                message="Dry run; no network request or download performed.",
                ydl_opts={"backend": plan.backend, "args": plan.args, "output": str(plan.output)},
            )
        backend_progress_callback = _curl_fallback_progress_callback(
            options,
            plan,
            progress_callback,
        )
        if _should_start_with_verified_curl_fallback(options, plan):
            return self._download_with_verified_curl_fallback(
                options,
                plan,
                progress_callback=progress_callback,
                message=f"{plan.backend} TLS probe failed; using verified curl",
            )
        try:
            if plan.backend == FileBackendChoice.aria2.value:
                return self._download_with_aria2(
                    options,
                    plan,
                    progress_callback=backend_progress_callback,
                )
            if plan.backend == FileBackendChoice.wget2.value:
                return self._download_with_wget2(
                    options,
                    plan,
                    progress_callback=backend_progress_callback,
                )
            return self._download_native(
                options,
                plan,
                progress_callback=backend_progress_callback,
            )
        except Exception as exc:
            fallback = self._download_with_curl_after_tls_failure(
                options,
                plan,
                exc,
                progress_callback=progress_callback,
            )
            if fallback is not None:
                return fallback
            raise

    def _resolve_backend(
        self,
        options: FileDownloadOptions,
        *,
        dry_run: bool,
    ) -> FileBackendChoice:
        selected = options.backend
        if _is_metalink_download(options):
            if selected in {FileBackendChoice.native, FileBackendChoice.wget2}:
                if dry_run:
                    return selected
                raise EngineError(
                    f"{selected.value.title()} file downloads cannot expand Metalink manifests. "
                    "Use --backend aria2 or pass --no-metalink to save the manifest itself."
                )
            if selected == FileBackendChoice.auto:
                if shutil.which("aria2c") or dry_run:
                    return FileBackendChoice.aria2
                raise DependencyMissingError(
                    "aria2c is required for Metalink downloads. "
                    "Install it with `brew install aria2` or pass --no-metalink."
                )
        if selected == FileBackendChoice.auto:
            return FileBackendChoice.aria2 if shutil.which("aria2c") else FileBackendChoice.native
        if selected == FileBackendChoice.aria2 and not shutil.which("aria2c"):
            if dry_run:
                return selected
            raise DependencyMissingError(
                "aria2c is not installed. Install it with `brew install aria2`."
            )
        if selected == FileBackendChoice.wget2 and not shutil.which("wget2"):
            if dry_run:
                return selected
            raise DependencyMissingError(
                "wget2 is not installed. Install it with `brew install wget2`."
            )
        return selected

    def _aria2_args(self, options: FileDownloadOptions, output: Path) -> list[str]:
        executable = shutil.which("aria2c") or "aria2c"
        args = [
            executable,
            "--dir",
            str(output.parent),
            f"--continue={'true' if options.continue_download else 'false'}",
            f"--allow-overwrite={'true' if options.overwrite else 'false'}",
            "--auto-file-renaming=false",
            f"--max-connection-per-server={options.connections}",
            f"--split={options.splits}",
            f"--min-split-size={options.chunk_size}",
            "--console-log-level=warn",
            "--summary-interval=1",
            "--show-console-readout=true",
            "--download-result=hide",
        ]
        if options.filename or not _is_metalink_download(options):
            args.extend(["--out", output.name])
        if _is_metalink_download(options):
            args.append("--follow-metalink=true")
        elif options.metalink is False:
            args.append("--follow-metalink=false")
        if options.rate_limit:
            args.append(f"--max-download-limit={options.rate_limit}")
        args.extend(_aria2_policy_args(options))
        checksum = parse_checksum(options.checksum)
        if checksum:
            algorithm, digest = checksum
            args.append(f"--checksum={_aria2_checksum_algorithm(algorithm)}={digest}")
        args.append(options.url)
        return args

    def _aria2_rpc_args_preview(self, options: FileDownloadOptions) -> list[str]:
        executable = shutil.which("aria2c") or "aria2c"
        return Aria2RpcSession.redacted_command(
            executable,
            input_file=options.input_file,
            save_session=options.save_session,
            save_session_interval=options.save_session_interval,
            server_stat_if=options.server_stat_if,
            server_stat_of=options.server_stat_of,
            server_stat_timeout=options.server_stat_timeout,
            uri_selector=options.uri_selector.value if options.uri_selector else None,
        )

    def _wget2_file_args(self, options: FileDownloadOptions, output: Path) -> list[str]:
        executable = shutil.which("wget2") or "wget2"
        args = [
            executable,
            "--no-verbose",
            "--progress=bar",
            "--force-progress",
            "--output-document",
            str(output),
            f"--max-threads={options.connections}",
        ]
        if options.continue_download:
            args.append("--continue")
        elif not options.overwrite:
            args.append("--no-clobber")
        if options.overwrite:
            args.append("--clobber")
        if options.timestamping:
            args.append("--timestamping")
        if not options.use_server_timestamps:
            args.append("--no-use-server-timestamps")
        if options.chunk_size:
            args.append(f"--chunk-size={options.chunk_size}")
        if options.rate_limit:
            args.append(f"--limit-rate={options.rate_limit}")
        if options.max_tries is not None:
            args.append(f"--tries={options.max_tries}")
        if options.retry_wait is not None:
            args.append(f"--waitretry={options.retry_wait:g}")
        args.append(f"--timeout={options.timeout:g}")
        if options.connect_timeout is not None:
            args.append(f"--connect-timeout={options.connect_timeout:g}")
        if options.user_agent:
            args.append(f"--user-agent={options.user_agent}")
        for header in options.headers:
            args.append(f"--header={header}")
        if options.referer:
            args.append(f"--referer={options.referer}")
        _append_bool_arg(args, options.cache, "--cache", "--no-cache")
        if options.no_compression:
            args.append("--no-compression")
        elif options.compression:
            args.append(f"--compression={options.compression}")
        if options.method and options.method != "GET":
            args.append(f"--method={options.method}")
        if options.body_data:
            args.append(f"--body-data={options.body_data}")
        if options.body_file:
            args.append(f"--body-file={options.body_file}")
        if options.load_cookies:
            args.append(f"--load-cookies={options.load_cookies}")
        if options.proxy:
            args.append(f"--http-proxy={options.proxy}")
            args.append(f"--https-proxy={options.proxy}")
        if options.http_user:
            args.append(f"--http-user={options.http_user}")
        if options.http_password:
            args.append(f"--http-password={options.http_password}")
        _append_bool_arg(
            args,
            options.check_certificate,
            "--check-certificate",
            "--no-check-certificate",
        )
        if options.ca_certificate:
            args.append(f"--ca-certificate={options.ca_certificate}")
        if options.ca_directory:
            args.append(f"--ca-directory={options.ca_directory}")
        if options.certificate:
            args.append(f"--certificate={options.certificate}")
        if options.private_key:
            args.append(f"--private-key={options.private_key}")
        if options.secure_protocol:
            args.append(f"--secure-protocol={options.secure_protocol}")
        if options.metalink is False:
            args.append("--no-metalink")
        args.append(options.url)
        return args

    def _download_with_aria2(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        *,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        executable = shutil.which("aria2c") or "aria2c"
        try:
            result = Aria2RpcSession(
                executable=executable,
                input_file=options.input_file,
                save_session=options.save_session,
                save_session_interval=options.save_session_interval,
                server_stat_if=options.server_stat_if,
                server_stat_of=options.server_stat_of,
                server_stat_timeout=options.server_stat_timeout,
                uri_selector=options.uri_selector.value if options.uri_selector else None,
            ).download(
                options,
                plan.output,
                progress_callback=progress_callback,
            )
        except Aria2RpcStartupError:
            return self._download_with_aria2_subprocess(
                options,
                plan,
                progress_callback=progress_callback,
            )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {result.output}",
            ydl_opts={
                "backend": plan.backend,
                "output": str(result.output),
                "metalink": _is_metalink_download(options),
            },
        )

    def _download_with_aria2_subprocess(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        *,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.aria2,
                status="starting",
                phase=ProgressPhase.download,
                kind=_aria2_progress_kind(options),
                filename=plan.output.name,
                url=options.url,
                message="starting aria2c",
            ),
        )

        def on_line(line: str) -> None:
            event = progress_event_from_aria2_line(
                line,
                filename=plan.output.name,
                url=options.url,
                kind=_aria2_progress_kind(options),
            )
            _emit(progress_callback, event)

        result = run_args_stream(plan.args, on_line=on_line, timeout=None)
        if result.returncode != 0:
            message = (
                result.stderr or result.stdout
            ).strip() or f"aria2c exited {result.returncode}"
            _emit(
                progress_callback,
                ProgressEvent(
                    engine=EngineKind.aria2,
                    status="error",
                    phase=ProgressPhase.error,
                    kind=_aria2_progress_kind(options),
                    filename=plan.output.name,
                    url=options.url,
                    message=message,
                ),
            )
            raise EngineError(message)
        downloaded = plan.output.stat().st_size if plan.output.exists() else None
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.aria2,
                status="done",
                phase=ProgressPhase.done,
                kind=_aria2_progress_kind(options),
                filename=plan.output.name,
                url=options.url,
                downloaded_bytes=downloaded,
                total_bytes=downloaded,
                message="aria2c finished",
            ),
        )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {plan.output}",
        )

    def _download_with_wget2(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        *,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        if _is_metalink_download(options):
            raise EngineError(
                "wget2 file downloads cannot expand Metalink manifests. "
                "Use --backend aria2 or pass --no-metalink to save the manifest itself."
            )
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.wget2,
                status="starting",
                phase=ProgressPhase.download,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                message="starting wget2",
            ),
        )

        def on_line(line: str) -> None:
            event = progress_event_from_wget2_line(
                line,
                filename=plan.output.name,
                url=options.url,
                kind=HubKind.file,
            )
            _emit(progress_callback, event)

        result = run_args_stream(plan.args, on_line=on_line, timeout=None)
        if result.returncode != 0:
            message = (
                result.stderr or result.stdout
            ).strip() or f"wget2 exited {result.returncode}"
            _emit(
                progress_callback,
                ProgressEvent(
                    engine=EngineKind.wget2,
                    status="error",
                    phase=ProgressPhase.error,
                    kind=HubKind.file,
                    filename=plan.output.name,
                    url=options.url,
                    message=message,
                ),
            )
            raise EngineError(message)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.wget2,
                status="running",
                phase=ProgressPhase.verify,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                message="verifying checksum" if options.checksum else "finalizing file",
            ),
        )
        verify_checksum(plan.output, options.checksum)
        downloaded = plan.output.stat().st_size if plan.output.exists() else None
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.wget2,
                status="done",
                phase=ProgressPhase.done,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                downloaded_bytes=downloaded,
                total_bytes=downloaded,
                message="wget2 finished",
            ),
        )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {plan.output}",
            ydl_opts={
                "backend": plan.backend,
                "output": str(plan.output),
            },
        )

    def _download_with_curl_after_tls_failure(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        failure: BaseException,
        *,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult | None:
        if not _can_use_verified_curl_fallback(options, plan.output, failure):
            return None
        curl = shutil.which("curl")
        if curl is None:
            return None
        return self._download_with_verified_curl_fallback(
            options,
            plan,
            progress_callback=progress_callback,
            message=f"{plan.backend} TLS chain failed; retrying with verified curl",
        )

    def _download_with_verified_curl_fallback(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        *,
        progress_callback: ProgressCallback | None,
        message: str,
    ) -> DownloadResult:
        curl = shutil.which("curl")
        if curl is None:
            raise EngineError("curl fallback is unavailable")
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.curl,
                status="downloading",
                phase=ProgressPhase.download,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                message=message,
            ),
        )
        args = _curl_file_args(curl, options, plan.output)
        result = run_args_stream(args, on_line=lambda _line: None, timeout=None)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or f"curl exited {result.returncode}"
            _emit(
                progress_callback,
                ProgressEvent(
                    engine=EngineKind.curl,
                    status="error",
                    phase=ProgressPhase.error,
                    kind=HubKind.file,
                    filename=plan.output.name,
                    url=options.url,
                    message=message,
                ),
            )
            raise EngineError(message)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.curl,
                status="running",
                phase=ProgressPhase.verify,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                message="verifying checksum" if options.checksum else "finalizing file",
            ),
        )
        verify_checksum(plan.output, options.checksum)
        downloaded = plan.output.stat().st_size if plan.output.exists() else None
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.curl,
                status="done",
                phase=ProgressPhase.done,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                downloaded_bytes=downloaded,
                total_bytes=downloaded,
                message="curl fallback finished",
            ),
        )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {plan.output} (curl TLS fallback)",
            ydl_opts={
                "backend": "curl",
                "fallback_from": plan.backend,
                "output": str(plan.output),
            },
        )

    def _download_native(
        self,
        options: FileDownloadOptions,
        plan: BackendPlan,
        *,
        progress_callback: ProgressCallback | None,
    ) -> DownloadResult:
        if _is_metalink_download(options):
            raise EngineError(
                "Native file downloads cannot expand Metalink manifests. "
                "Use --backend aria2 or pass --no-metalink to save the manifest itself."
            )
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        resume_from = 0
        open_mode = "wb"
        probe = options.probe
        metadata = _read_http_metadata(plan.output)
        if plan.output.exists() and not options.overwrite:
            local_size = plan.output.stat().st_size
            if options.timestamping and _local_file_is_current(plan.output, probe):
                verify_checksum(plan.output, options.checksum)
                _emit_native_skip(
                    progress_callback,
                    options,
                    plan,
                    message="local file is current",
                )
                return DownloadResult(
                    status=DownloadStatus.skipped,
                    url=options.url,
                    message=f"Local file is current: {plan.output}",
                )
            if options.timestamping:
                open_mode = "wb"
            if not options.continue_download and not options.timestamping:
                raise EngineError(f"Output file already exists: {plan.output}")
            if local_size > 0 and not options.timestamping:
                if probe and probe.content_length is not None:
                    if local_size == probe.content_length:
                        verify_checksum(plan.output, options.checksum)
                        _emit_native_skip(
                            progress_callback,
                            options,
                            plan,
                            message="file already complete",
                        )
                        return DownloadResult(
                            status=DownloadStatus.skipped,
                            url=options.url,
                            message=f"Already complete: {plan.output}",
                        )
                    if local_size > probe.content_length:
                        raise EngineError(
                            f"Output file is larger than the remote file: {plan.output}"
                        )
                _require_native_resume_support(plan.output, probe)
                resume_from = local_size
                open_mode = "ab"

        headers = _native_request_headers(
            options,
            plan.output,
            probe,
            metadata,
            resume_from=resume_from,
        )
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        body = _request_body(options.body_data, options.body_file)
        request = Request(options.url, data=body, headers=headers, method=options.method)
        started = time.monotonic()
        rate_limiter = _NativeRateLimiter.from_limit(options.rate_limit)
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.native,
                status="starting",
                phase=ProgressPhase.download,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
            ),
        )
        remote_last_modified = probe.last_modified if probe else None
        remote_etag = probe.etag if probe else None
        response_headers: Mapping[str, str] = {}
        response_final_url: str | None = None
        total: int | None = None
        completed = resume_from
        try:
            context = _native_ssl_context(options)
            response_cm = (
                urlopen(request, timeout=options.timeout)
                if context is None
                else urlopen(request, timeout=options.timeout, context=context)
            )
            with response_cm as response:
                status = _response_status(response)
                response_final_url = _response_url(response)
                if resume_from and status != 206:
                    raise EngineError(
                        "Server did not honor the byte-range resume request; "
                        "refusing to append a full response."
                    )
                remote_last_modified = response.headers.get("Last-Modified") or remote_last_modified
                remote_etag = response.headers.get("ETag") or remote_etag
                response_headers = dict(response.headers.items())
                length_header = response.headers.get("Content-Length")
                response_length = (
                    int(length_header) if length_header and length_header.isdigit() else None
                )
                total = (
                    probe.content_length
                    if probe and probe.content_length is not None
                    else (
                        resume_from + response_length
                        if resume_from and response_length is not None
                        else response_length
                    )
                )
                with plan.output.open(open_mode) as fh:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        completed += len(chunk)
                        rate_limiter.throttle(len(chunk))
                        if progress_callback:
                            elapsed = max(time.monotonic() - started, 0.001)
                            speed = (completed - resume_from) / elapsed
                            eta = ((total - completed) / speed) if total and speed > 0 else None
                            progress_callback(
                                ProgressEvent(
                                    engine=EngineKind.native,
                                    status="downloading",
                                    phase=ProgressPhase.download,
                                    kind=HubKind.file,
                                    filename=plan.output.name,
                                    url=options.url,
                                    downloaded_bytes=completed,
                                    total_bytes=total,
                                    speed_bytes_per_sec=speed,
                                    eta_seconds=eta,
                                )
                            )
                _assert_native_download_complete(
                    plan.output,
                    completed=completed,
                    total=total,
                )
        except HTTPError as exc:
            if exc.code == 304 and options.timestamping:
                _write_http_metadata(
                    plan.output,
                    url=options.url,
                    headers=dict(exc.headers.items()),
                    fallback={
                        **metadata,
                        "final_url": getattr(exc, "url", None) or metadata.get("final_url"),
                    },
                )
                _emit_native_skip(
                    progress_callback,
                    options,
                    plan,
                    message="remote returned not modified",
                )
                return DownloadResult(
                    status=DownloadStatus.skipped,
                    url=options.url,
                    message=f"Local file is current: {plan.output}",
                )
            raise EngineError(str(exc)) from exc
        if options.use_server_timestamps:
            _apply_server_timestamp(plan.output, remote_last_modified)
        _write_http_metadata(
            plan.output,
            url=options.url,
            headers=response_headers,
            fallback={
                **metadata,
                "etag": remote_etag,
                "last_modified": remote_last_modified,
                "final_url": response_final_url,
            },
        )
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.native,
                status="running",
                phase=ProgressPhase.verify,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                message="verifying checksum" if options.checksum else "finalizing file",
            ),
        )
        verify_checksum(plan.output, options.checksum)
        downloaded = plan.output.stat().st_size if plan.output.exists() else completed
        _emit(
            progress_callback,
            ProgressEvent(
                engine=EngineKind.native,
                status="done",
                phase=ProgressPhase.done,
                kind=HubKind.file,
                filename=plan.output.name,
                url=options.url,
                downloaded_bytes=downloaded,
                total_bytes=total or downloaded,
            ),
        )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {plan.output}",
        )


class SiteMirrorEngine:
    """Mirror websites through wget2 or wget subprocess backends."""

    def plan(
        self,
        options: SiteDownloadOptions,
        *,
        stats_dir: Path | None = None,
    ) -> BackendPlan:
        backend = self._resolve_backend(options.backend, dry_run=options.dry_run)
        executable = shutil.which(backend.value) or backend.value
        is_wget2 = backend == SiteBackendChoice.wget2
        warnings = _mirror_safety_warnings(options)
        if is_wget2:
            warnings.extend(_wget2_capability_warnings(options))
        directory_wget2_baseline = is_wget2 and isinstance(options, DirectoryMirrorOptions)
        args = [executable, "--recursive"]
        if directory_wget2_baseline:
            if options.no_parent:
                args.append("--no-parent")
            args.append("--mirror")
            if options.continue_download:
                args.append("--continue")
            if options.timestamping:
                args.append("--timestamping")
            if options.if_modified_since is not None:
                args.append(
                    "--if-modified-since"
                    if options.if_modified_since
                    else "--no-if-modified-since"
                )
            args.append(f"--directory-prefix={options.output_dir}")
            if options.user_agent:
                args.append(f"--user-agent={options.user_agent}")
        else:
            args.append(f"--directory-prefix={options.output_dir}")
        args.extend([f"--level={options.depth}", "--no-verbose"])
        if options.page_requisites:
            args.append("--page-requisites")
        if options.convert_links:
            args.append("--convert-links")
        if options.span_hosts:
            args.append("--span-hosts")
        if options.wait is not None:
            args.append(f"--wait={options.wait:g}")
        if options.accept:
            args.append(f"--accept={options.accept}")
        if options.reject:
            args.append(f"--reject={options.reject}")
        if is_wget2:
            args.append("--robots" if options.robots else "--no-robots")
            args.append("--follow-sitemaps" if options.follow_sitemaps else "--no-follow-sitemaps")
            if options.max_threads:
                args.append(f"--max-threads={options.max_threads}")
            if options.filter_mime_type:
                args.append(f"--filter-mime-type={options.filter_mime_type}")
            if options.filter_urls:
                args.append("--filter-urls")
            if options.follow_tags:
                args.append(f"--follow-tags={options.follow_tags}")
            if options.ignore_tags:
                args.append(f"--ignore-tags={options.ignore_tags}")
        elif not options.robots:
            args.extend(["--execute", "robots=off"])
        if options.no_parent and not directory_wget2_baseline:
            args.append("--no-parent")
        if options.domains:
            args.append(f"--domains={options.domains}")
        if options.exclude_domains:
            args.append(f"--exclude-domains={options.exclude_domains}")
        if options.include_directories:
            args.append(f"--include-directories={options.include_directories}")
        if options.exclude_directories:
            args.append(f"--exclude-directories={options.exclude_directories}")
        if options.accept_regex:
            args.append(f"--accept-regex={options.accept_regex}")
        if options.reject_regex:
            args.append(f"--reject-regex={options.reject_regex}")
        if options.ignore_case:
            args.append("--ignore-case")
        _append_bool_arg(args, options.directories, "--directories", "--no-directories")
        _append_bool_arg(
            args,
            options.host_directories,
            "--host-directories",
            "--no-host-directories",
        )
        _append_bool_arg(
            args,
            options.protocol_directories,
            "--protocol-directories",
            "--no-protocol-directories",
        )
        if options.cut_dirs is not None:
            args.append(f"--cut-dirs={options.cut_dirs}")
        if options.default_page:
            args.append(f"--default-page={options.default_page}")
        if options.adjust_extension:
            args.append("--adjust-extension")
        if is_wget2:
            if options.convert_file_only:
                args.append("--convert-file-only")
            if options.cut_url_get_vars:
                args.append("--cut-url-get-vars")
            if options.cut_file_get_vars:
                args.append("--cut-file-get-vars")
            if options.keep_extension:
                args.append("--keep-extension")
            if options.unlink:
                args.append("--unlink")
        if options.backups is not None:
            args.append(f"--backups={options.backups}")
        if options.backup_converted:
            args.append("--backup-converted")
        if options.restrict_file_names:
            args.append(f"--restrict-file-names={options.restrict_file_names}")
        if options.download_attr:
            args.append(f"--download-attr={options.download_attr.value}")
        if options.input_file:
            args.append(f"--input-file={options.input_file}")
        if options.base:
            args.append(f"--base={options.base}")
        if is_wget2:
            if options.force_html:
                args.append("--force-html")
            if options.force_css:
                args.append("--force-css")
            if options.force_sitemap:
                args.append("--force-sitemap")
            if options.force_atom:
                args.append("--force-atom")
            if options.force_rss:
                args.append("--force-rss")
            if options.force_metalink:
                args.append("--force-metalink")
        args.extend(
            _wget_common_policy_args(
                options,
                is_wget2=is_wget2,
                include_user_agent=not directory_wget2_baseline,
            )
        )
        args.append(f"--tries={options.tries}")
        args.append(f"--waitretry={options.waitretry:g}")
        if options.retry_on_http_error:
            args.append(f"--retry-on-http-error={options.retry_on_http_error}")
        args.append(f"--max-redirect={options.max_redirect}")
        if options.timeout is not None:
            args.append(f"--timeout={options.timeout:g}")
        if options.dns_timeout is not None:
            args.append(f"--dns-timeout={options.dns_timeout:g}")
        if options.connect_timeout is not None:
            args.append(f"--connect-timeout={options.connect_timeout:g}")
        if options.read_timeout is not None:
            args.append(f"--read-timeout={options.read_timeout:g}")
        if options.random_wait:
            args.append("--random-wait")
        if options.timestamping and not directory_wget2_baseline:
            args.append("--timestamping")
        if (
            is_wget2
            and options.if_modified_since is not None
            and not directory_wget2_baseline
        ):
            args.append(
                "--if-modified-since"
                if options.if_modified_since
                else "--no-if-modified-since"
            )
        if options.continue_download and not directory_wget2_baseline:
            args.append("--continue")
        if options.overwrite and is_wget2:
            args.append("--clobber")
        if options.spider:
            args.append("--spider")
        if options.warc_file:
            args.append(f"--warc-file={options.warc_file}")
        _append_bool_arg(
            args,
            options.warc_compression,
            "--warc-compression",
            "--no-warc-compression",
        )
        if options.warc_cdx:
            args.append("--warc-cdx")
        if options.warc_max_size:
            args.append(f"--warc-max-size={options.warc_max_size}")
        if options.inet4_only and options.inet6_only:
            raise EngineError("--inet4-only and --inet6-only cannot be used together")
        if options.inet4_only:
            args.append("--inet4-only")
        if options.inet6_only:
            args.append("--inet6-only")
        if options.bind_address:
            args.append(f"--bind-address={options.bind_address}")
        if is_wget2:
            if options.bind_interface:
                args.append(f"--bind-interface={options.bind_interface}")
            if options.prefer_family:
                args.append(f"--prefer-family={options.prefer_family.value}")
            _append_bool_arg(args, options.dns_cache, "--dns-cache", "--no-dns-cache")
            if options.dns_cache_preload:
                args.append(f"--dns-cache-preload={options.dns_cache_preload}")
            _append_bool_arg(args, options.tcp_fastopen, "--tcp-fastopen", "--no-tcp-fastopen")
        stats_files = (
            _wget2_stats_files(stats_dir) if is_wget2 and options.stats and stats_dir else None
        )
        if stats_files:
            args.extend(_wget2_stats_args(stats_files))
        if options.input_file_only and not options.input_file:
            raise EngineError("input-file-only site plans require --input-file")
        if not options.input_file_only:
            args.append(options.url)
        return BackendPlan(
            backend=backend.value,
            args=args,
            output=options.output_dir,
            stats_files=stats_files,
            warnings=warnings,
        )

    def mirror(
        self,
        options: SiteDownloadOptions,
        *,
        progress_callback: ProgressCallback | None = None,
        control: ProcessControl | None = None,
    ) -> DownloadResult:
        if options.dry_run:
            plan = self.plan(options)
            return DownloadResult(
                status=DownloadStatus.dry_run,
                url=options.url,
                message="Dry run; no network request or download performed.",
                ydl_opts={
                    "backend": plan.backend,
                    "args": plan.args,
                    "output": str(plan.output),
                    "warnings": plan.warnings,
                },
            )
        with tempfile.TemporaryDirectory(prefix="atlas-wget2-") as runtime_tmp:
            runtime_options = options
            if options.browser_cookies:
                if options.load_cookies:
                    raise EngineError(
                        "Use either --load-cookies or --cookies-from-browser, not both"
                    )
                cookies_path = _export_browser_cookies_to_file(
                    options.browser_cookies,
                    Path(runtime_tmp),
                )
                runtime_options = options.model_copy(update={"load_cookies": cookies_path})
            plan = self.plan(runtime_options, stats_dir=Path(runtime_tmp))
            plan.output.mkdir(parents=True, exist_ok=True)
            engine_kind = EngineKind.wget if plan.backend == "wget" else EngineKind.wget2
            mirror_kind = _mirror_hub_kind(runtime_options)
            _emit(
                progress_callback,
                ProgressEvent(
                    engine=engine_kind,
                    status="starting",
                    phase=ProgressPhase.extract,
                    kind=mirror_kind,
                    filename=str(plan.output),
                    url=runtime_options.url,
                    message=f"starting {plan.backend}",
                ),
            )

            def on_line(line: str) -> None:
                event = progress_event_from_wget2_line(
                    line,
                    filename=str(plan.output),
                    url=runtime_options.url,
                    kind=mirror_kind,
                )
                if event and engine_kind == EngineKind.wget:
                    event = event.model_copy(update={"engine": EngineKind.wget})
                _emit(progress_callback, event)

            try:
                stream_kwargs: dict[str, Any] = {
                    "on_line": on_line,
                    "timeout": runtime_options.max_runtime,
                }
                if control is not None:
                    stream_kwargs["control"] = control
                result = run_args_stream(plan.args, **stream_kwargs)
            except ProcessCanceled as exc:
                message = f"Mirror canceled: {exc.reason}"
                _emit(
                    progress_callback,
                    ProgressEvent(
                        engine=engine_kind,
                        status="canceled",
                        phase=ProgressPhase.error,
                        kind=mirror_kind,
                        filename=str(plan.output),
                        url=runtime_options.url,
                        message=message,
                    ),
                )
                raise EngineError(message) from exc
            except subprocess.TimeoutExpired as exc:
                stats = parse_wget2_stats_files(plan.stats_files or {})
                message = f"Mirror stopped after max runtime of {exc.timeout:g} seconds."
                _emit(
                    progress_callback,
                    ProgressEvent(
                        engine=engine_kind,
                        status="error",
                        phase=ProgressPhase.error,
                        kind=mirror_kind,
                        filename=str(plan.output),
                        url=runtime_options.url,
                        message=message,
                    ),
                )
                raise EngineError(message) from exc
            stats = parse_wget2_stats_files(plan.stats_files or {})
            if result.returncode != 0:
                base_message = (
                    result.stderr or result.stdout
                ).strip() or f"{plan.backend} exited {result.returncode}"
                message = _wget2_error_message(base_message, stats)
                _emit(
                    progress_callback,
                    ProgressEvent(
                        engine=engine_kind,
                        status="error",
                        phase=ProgressPhase.error,
                        kind=mirror_kind,
                        filename=str(plan.output),
                        url=runtime_options.url,
                        message=message,
                    ),
                )
                raise EngineError(message)
            _emit(
                progress_callback,
                ProgressEvent(
                    engine=engine_kind,
                    status="done",
                    phase=ProgressPhase.done,
                    kind=mirror_kind,
                    filename=str(plan.output),
                    url=runtime_options.url,
                    message=f"{plan.backend} finished",
                ),
            )
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message=f"Saved under {plan.output}",
                ydl_opts={
                    "backend": plan.backend,
                    "output": str(plan.output),
                    "stats": stats,
                    "warnings": plan.warnings,
                },
            )

    def _resolve_backend(
        self,
        selected: SiteBackendChoice,
        *,
        dry_run: bool,
    ) -> SiteBackendChoice:
        if selected == SiteBackendChoice.auto:
            if shutil.which("wget2"):
                return SiteBackendChoice.wget2
            if shutil.which("wget"):
                return SiteBackendChoice.wget
            if dry_run:
                return SiteBackendChoice.wget2
            raise DependencyMissingError(
                "wget2 or wget is not installed. Install wget2 with `brew install wget2` "
                "or wget with `brew install wget`."
            )
        if not shutil.which(selected.value):
            if dry_run:
                return selected
            raise DependencyMissingError(
                f"{selected.value} is not installed. "
                f"Install it with `brew install {selected.value}`."
            )
        return selected


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    return safe_filename(parsed.path, default="download")


def _safe_output_path(output_dir: Path, filename: str) -> Path:
    output = output_dir / safe_filename(filename, default="download")
    if output.is_symlink():
        raise EngineError(f"Refusing to write through symlink: {output}")
    return output


def parse_checksum(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    algorithm, digest = value.split(":", 1)
    return algorithm, digest


def verify_checksum(path: Path, checksum: str | None) -> None:
    parsed = parse_checksum(checksum)
    if parsed is None:
        return
    algorithm, expected = parsed
    digest = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise EngineError(f"Checksum mismatch for {path.name}: expected {expected}, got {actual}")


def parse_wget2_stats_files(files: Mapping[str, Path]) -> dict[str, Any]:
    """Parse wget2 stats files into a compact JSON-friendly mapping."""

    parsed: dict[str, Any] = {}
    for label, path in files.items():
        if not path.exists() or path.stat().st_size == 0:
            continue
        parsed[label] = _parse_wget2_stats_file(path, label=label)
    summary = _summarize_wget2_stats(parsed)
    if summary:
        parsed["summary"] = summary
    return parsed


def _wget2_error_message(base_message: str, stats: Mapping[str, Any]) -> str:
    details = _wget2_error_details(stats)
    if not details:
        return base_message
    return f"{base_message}; {details}"


def _wget2_error_details(stats: Mapping[str, Any]) -> str:
    parts: list[str] = []
    summary = stats.get("summary")
    site = summary.get("site") if isinstance(summary, Mapping) else None
    if isinstance(site, Mapping):
        downloaded = site.get("downloaded_bytes")
        if isinstance(downloaded, int) and downloaded > 0:
            parts.append(f"downloaded {_compact_bytes(downloaded)} before exit")
        failures = site.get("failures")
        if isinstance(failures, int) and failures > 0:
            parts.append(f"{failures} failed URL{'s' if failures != 1 else ''}")
    failed_urls = _wget2_failed_urls(stats)
    if failed_urls:
        shown = "; ".join(failed_urls[:5])
        suffix = f"; +{len(failed_urls) - 5} more" if len(failed_urls) > 5 else ""
        parts.append(f"failed URL samples: {shown}{suffix}")
    return "; ".join(parts)


def _wget2_failed_urls(stats: Mapping[str, Any]) -> list[str]:
    failed: list[str] = []
    for row in _stats_rows(stats.get("site")):
        status = _int_or_none(_row_lookup(row, "status", "Status"))
        if status is None or status < 400:
            continue
        url = _row_lookup(row, "url", "URL")
        if url:
            failed.append(f"{status} {url}")
    return failed


def _compact_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def _is_metalink_download(options: FileDownloadOptions) -> bool:
    return options.force_metalink or (options.metalink and is_metalink_url(options.url))


_TLS_CERT_FAILURE_MARKERS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "certificate is not trusted",
    "tls certificate verification failed",
    "unable to get local issuer certificate",
    "unable to locally verify the issuer",
    "ssl/tls handshake failure",
)


def is_tls_certificate_failure(value: BaseException | str | None) -> bool:
    if value is None:
        return False
    if isinstance(value, ssl.SSLCertVerificationError):
        return True
    if isinstance(value, URLError):
        reason = getattr(value, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        if isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return True
    text = str(value).lower()
    return any(marker in text for marker in _TLS_CERT_FAILURE_MARKERS)


def _can_use_verified_curl_fallback(
    options: FileDownloadOptions,
    output: Path,
    failure: BaseException,
) -> bool:
    return is_tls_certificate_failure(failure) and can_attempt_verified_curl_fallback(
        options,
        output,
    )


def can_attempt_verified_curl_fallback(
    options: FileDownloadOptions,
    output: Path,
) -> bool:
    if options.check_certificate is False:
        return False
    if _is_metalink_download(options):
        return False
    if options.method != "GET" or options.body_data or options.body_file:
        return False
    if output.exists() and not options.overwrite and not options.continue_download:
        return False
    return not options.secure_protocol


def _curl_fallback_progress_callback(
    options: FileDownloadOptions,
    plan: BackendPlan,
    progress_callback: ProgressCallback | None,
) -> ProgressCallback | None:
    if progress_callback is None:
        return None
    if not can_attempt_verified_curl_fallback(options, plan.output):
        return progress_callback
    if shutil.which("curl") is None:
        return progress_callback

    def callback(event: ProgressEvent) -> None:
        if event.status in {"error", "failed"} and is_tls_certificate_failure(event.message):
            event = event.model_copy(
                update={
                    "engine": EngineKind.curl,
                    "status": "retrying",
                    "phase": ProgressPhase.download,
                    "error_code": None,
                    "message": f"{plan.backend} TLS chain failed; trying verified curl fallback",
                }
            )
        progress_callback(event)

    return callback


def _should_start_with_verified_curl_fallback(
    options: FileDownloadOptions,
    plan: BackendPlan,
) -> bool:
    if not can_attempt_verified_curl_fallback(options, plan.output):
        return False
    if shutil.which("curl") is None:
        return False
    probe = options.probe
    return probe is not None and is_tls_certificate_failure(probe.error)


def _curl_file_args(curl: str, options: FileDownloadOptions, output: Path) -> list[str]:
    args = [
        curl,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--output",
        str(output),
    ]
    if options.continue_download and output.exists() and output.stat().st_size > 0:
        args.extend(["--continue-at", "-"])
    if options.timeout:
        args.extend(["--max-time", f"{options.timeout:g}"])
    if options.connect_timeout is not None:
        args.extend(["--connect-timeout", f"{options.connect_timeout:g}"])
    if options.rate_limit:
        args.extend(["--limit-rate", options.rate_limit])
    if options.user_agent:
        args.extend(["--user-agent", options.user_agent])
    if options.referer:
        args.extend(["--referer", options.referer])
    for header in options.headers:
        args.extend(["--header", header])
    if options.cache is False:
        args.extend(["--header", "Cache-Control: no-cache"])
    if options.no_compression:
        args.extend(["--header", "Accept-Encoding: identity"])
    else:
        args.append("--compressed")
    if options.load_cookies:
        args.extend(["--cookie", str(options.load_cookies)])
    if options.proxy:
        args.extend(["--proxy", options.proxy])
    if options.http_user:
        credential = f"{options.http_user}:{options.http_password or ''}"
        args.extend(["--user", credential])
    if options.ca_certificate:
        args.extend(["--cacert", str(options.ca_certificate)])
    if options.ca_directory:
        args.extend(["--capath", str(options.ca_directory)])
    if options.certificate:
        args.extend(["--cert", str(options.certificate)])
    if options.private_key:
        args.extend(["--key", str(options.private_key)])
    if options.use_server_timestamps:
        args.append("--remote-time")
    if options.max_tries is not None:
        args.extend(["--retry", str(options.max_tries)])
    if options.retry_wait is not None:
        args.extend(["--retry-delay", f"{options.retry_wait:g}"])
    args.append(options.url)
    return args


def _aria2_progress_kind(options: FileDownloadOptions) -> HubKind:
    return HubKind.manifest if _is_metalink_download(options) else HubKind.file


def _metadata_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.atlas-http.json")


def _read_http_metadata(path: Path) -> dict[str, str]:
    metadata_path = _metadata_path(path)
    if not metadata_path.exists():
        return {}
    try:
        decoded = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items() if value is not None}


def _write_http_metadata(
    path: Path,
    *,
    url: str,
    headers: Mapping[str, str],
    fallback: Mapping[str, object] | None = None,
) -> None:
    if not path.exists():
        return
    fallback = fallback or {}
    data: dict[str, object] = {
        "url": redact_url(url),
        "saved_at": int(time.time()),
    }
    for key in ("etag", "last_modified", "content_length", "content_type", "final_url"):
        value = fallback.get(key)
        if value:
            data[key] = redact_url(str(value)) if key == "final_url" else value
    header_map = _casefold_headers(headers)
    header_values = {
        "etag": header_map.get("etag"),
        "last_modified": header_map.get("last-modified"),
        "content_length": header_map.get("content-length"),
        "content_type": header_map.get("content-type"),
    }
    for key, value in header_values.items():
        if value:
            data[key] = value
    try:
        replace_private_text(
            _metadata_path(path),
            json.dumps(data, indent=2, sort_keys=True) + "\n",
        )
    except OSError:
        return


def _native_request_headers(
    options: FileDownloadOptions,
    path: Path,
    probe: object | None,
    metadata: Mapping[str, str],
    *,
    resume_from: int,
) -> dict[str, str]:
    headers: dict[str, str] = {"User-Agent": options.user_agent or "atlas/0.1"}
    if options.referer:
        headers["Referer"] = options.referer
    if options.cache is False:
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
    if options.no_compression:
        headers["Accept-Encoding"] = "identity"
    elif options.compression:
        headers["Accept-Encoding"] = options.compression
    headers.update(_headers_from_user_options(options.headers))
    if options.timestamping and path.exists() and not resume_from:
        etag = metadata.get("etag") or str(getattr(probe, "etag", "") or "")
        last_modified = metadata.get("last_modified") or str(
            getattr(probe, "last_modified", "") or ""
        )
        if etag:
            headers.setdefault("If-None-Match", etag)
        if not last_modified:
            last_modified = formatdate(path.stat().st_mtime, usegmt=True)
        headers.setdefault("If-Modified-Since", last_modified)
    return headers


def _request_body(body_data: str | None, body_file: Path | None) -> bytes | None:
    if body_data is not None:
        return body_data.encode("utf-8")
    if body_file is None:
        return None
    try:
        return body_file.read_bytes()
    except OSError as exc:
        raise EngineError(f"Could not read request body file {body_file}: {exc}") from exc


def _native_ssl_context(options: FileDownloadOptions) -> ssl.SSLContext | None:
    if options.check_certificate is False:
        context = ssl._create_unverified_context()
    elif options.ca_certificate or options.ca_directory or options.certificate:
        context = ssl.create_default_context(
            cafile=str(options.ca_certificate) if options.ca_certificate else None,
            capath=str(options.ca_directory) if options.ca_directory else None,
        )
    else:
        context = None
    if context and options.certificate:
        context.load_cert_chain(
            certfile=str(options.certificate),
            keyfile=str(options.private_key) if options.private_key else None,
        )
    return context


def _headers_from_user_options(headers: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for header in headers:
        name, _separator, value = header.partition(":")
        parsed[name.strip()] = value.strip()
    return parsed


def _casefold_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


class _NativeRateLimiter:
    def __init__(self, bytes_per_second: int | None) -> None:
        self._bytes_per_second = bytes_per_second
        self._started = time.monotonic()
        self._transferred = 0

    @classmethod
    def from_limit(cls, value: str | None) -> _NativeRateLimiter:
        return cls(_parse_byte_rate(value))

    def throttle(self, chunk_size: int) -> None:
        if not self._bytes_per_second:
            return
        self._transferred += chunk_size
        target_elapsed = self._transferred / self._bytes_per_second
        actual_elapsed = time.monotonic() - self._started
        if target_elapsed > actual_elapsed:
            time.sleep(target_elapsed - actual_elapsed)


def _parse_byte_rate(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned.endswith("/s"):
        cleaned = cleaned[:-2]
    if cleaned.endswith("ps"):
        cleaned = cleaned[:-2]
    if cleaned.endswith("ib"):
        cleaned = cleaned[:-2]
    elif cleaned.endswith("b"):
        cleaned = cleaned[:-1]
    multiplier = 1
    if cleaned and cleaned[-1] in {"k", "m", "g", "t"}:
        unit = cleaned[-1]
        cleaned = cleaned[:-1]
        multiplier = {
            "k": 1024,
            "m": 1024**2,
            "g": 1024**3,
            "t": 1024**4,
        }[unit]
    try:
        parsed = float(cleaned)
    except ValueError as exc:
        raise EngineError(
            "Native --rate-limit must be a positive byte rate such as 512K or 1M."
        ) from exc
    bytes_per_second = int(parsed * multiplier)
    if bytes_per_second <= 0:
        raise EngineError("Native --rate-limit must be a positive byte rate such as 512K or 1M.")
    return bytes_per_second


def _aria2_policy_args(options: FileDownloadOptions) -> list[str]:
    args: list[str] = []
    if options.user_agent:
        args.append(f"--user-agent={options.user_agent}")
    for header in options.headers:
        args.append(f"--header={header}")
    if options.referer:
        args.append(f"--referer={options.referer}")
    if options.cache is False:
        args.append("--header=Cache-Control: no-cache")
    if options.no_compression:
        args.append("--http-accept-gzip=false")
        args.append("--header=Accept-Encoding: identity")
    elif options.compression:
        args.append(f"--header=Accept-Encoding: {options.compression}")
    if options.method and options.method != "GET":
        args.append(f"--method={options.method}")
    if options.body_data:
        args.append(f"--body-data={options.body_data}")
    if options.body_file:
        args.append(f"--body-file={options.body_file}")
    if options.load_cookies:
        args.append(f"--load-cookies={options.load_cookies}")
    if options.proxy:
        args.append(f"--all-proxy={options.proxy}")
    if options.http_user:
        args.append(f"--http-user={options.http_user}")
    if options.http_password:
        args.append(f"--http-passwd={options.http_password}")
    if options.check_certificate is not None:
        args.append(f"--check-certificate={'true' if options.check_certificate else 'false'}")
    if options.ca_certificate:
        args.append(f"--ca-certificate={options.ca_certificate}")
    if options.certificate:
        args.append(f"--certificate={options.certificate}")
    if options.private_key:
        args.append(f"--private-key={options.private_key}")
    if options.secure_protocol:
        args.append(f"--secure-protocol={options.secure_protocol}")
    if options.input_file:
        args.append(f"--input-file={options.input_file}")
    if options.save_session:
        args.append(f"--save-session={options.save_session}")
        args.append("--force-save=true")
    if options.save_session_interval is not None:
        args.append(f"--save-session-interval={options.save_session_interval}")
    if options.metalink_preferred_protocol:
        args.append(f"--metalink-preferred-protocol={options.metalink_preferred_protocol.value}")
    if options.metalink_language:
        args.append(f"--metalink-language={options.metalink_language}")
    if options.metalink_os:
        args.append(f"--metalink-os={options.metalink_os}")
    if options.metalink_location:
        args.append(f"--metalink-location={options.metalink_location}")
    if options.metalink_base_uri:
        args.append(f"--metalink-base-uri={options.metalink_base_uri}")
    if options.metalink_enable_unique_protocol is not None:
        args.append(
            "--metalink-enable-unique-protocol="
            f"{'true' if options.metalink_enable_unique_protocol else 'false'}"
        )
    if options.server_stat_if:
        args.append(f"--server-stat-if={options.server_stat_if}")
    if options.server_stat_of:
        args.append(f"--server-stat-of={options.server_stat_of}")
    if options.server_stat_timeout is not None:
        args.append(f"--server-stat-timeout={options.server_stat_timeout}")
    if options.uri_selector:
        args.append(f"--uri-selector={options.uri_selector.value}")
    if options.lowest_speed_limit:
        args.append(f"--lowest-speed-limit={options.lowest_speed_limit}")
    if options.max_tries is not None:
        args.append(f"--max-tries={options.max_tries}")
    if options.retry_wait is not None:
        args.append(f"--retry-wait={options.retry_wait:g}")
    args.append(f"--timeout={options.timeout:g}")
    if options.connect_timeout is not None:
        args.append(f"--connect-timeout={options.connect_timeout:g}")
    if options.file_allocation:
        args.append(f"--file-allocation={options.file_allocation}")
    if options.check_integrity:
        args.append("--check-integrity=true")
    if options.remote_time:
        args.append("--remote-time=true")
    if options.conditional_get:
        args.append("--conditional-get=true")
    if not options.http_accept_gzip and not options.no_compression:
        args.append("--http-accept-gzip=false")
    return args


def _wget_common_policy_args(
    options: SiteDownloadOptions,
    *,
    is_wget2: bool,
    include_user_agent: bool = True,
) -> list[str]:
    args: list[str] = []
    if options.user_agent and include_user_agent:
        args.append(f"--user-agent={options.user_agent}")
    for header in options.headers:
        args.append(f"--header={header}")
    if options.referer:
        args.append(f"--referer={options.referer}")
    _append_bool_arg(args, options.cache, "--cache", "--no-cache")
    if options.no_compression:
        args.append("--no-compression")
    elif options.compression:
        args.append(f"--compression={options.compression}")
    if is_wget2:
        if options.method:
            args.append(f"--method={options.method}")
        if options.body_data:
            args.append(f"--body-data={options.body_data}")
        if options.body_file:
            args.append(f"--body-file={options.body_file}")
    if options.post_data:
        args.append(f"--post-data={options.post_data}")
    if options.post_file:
        args.append(f"--post-file={options.post_file}")
    if is_wget2:
        _append_bool_arg(args, options.cookies, "--cookies", "--no-cookies")
    if options.load_cookies:
        args.append(f"--load-cookies={options.load_cookies}")
    if options.save_cookies:
        args.append(f"--save-cookies={options.save_cookies}")
    if options.keep_session_cookies:
        args.append("--keep-session-cookies")
    if is_wget2 and options.cookie_suffixes:
        args.append(f"--cookie-suffixes={options.cookie_suffixes}")
    _append_bool_arg(args, options.netrc, "--netrc", "--no-netrc")
    if options.netrc_file:
        args.append(f"--netrc-file={options.netrc_file}")
    _append_bool_arg(args, options.proxy, "--proxy", "--no-proxy")
    if options.http_user:
        args.append(f"--http-user={options.http_user}")
    if options.http_password:
        args.append(f"--http-password={options.http_password}")
    if options.proxy_user:
        args.append(f"--proxy-user={options.proxy_user}")
    if options.proxy_password:
        args.append(f"--proxy-password={options.proxy_password}")
    if options.https_only:
        args.append("--https-only")
    if is_wget2 and options.https_enforce:
        args.append(f"--https-enforce={options.https_enforce.value}")
    if is_wget2:
        _append_bool_arg(args, options.hsts, "--hsts", "--no-hsts")
        if options.hsts_file:
            args.append(f"--hsts-file={options.hsts_file}")
    _append_bool_arg(
        args,
        options.check_certificate,
        "--check-certificate",
        "--no-check-certificate",
    )
    if is_wget2:
        _append_bool_arg(
            args,
            options.check_hostname,
            "--check-hostname",
            "--no-check-hostname",
        )
    if options.ca_certificate:
        args.append(f"--ca-certificate={options.ca_certificate}")
    if options.ca_directory:
        args.append(f"--ca-directory={options.ca_directory}")
    if options.certificate:
        args.append(f"--certificate={options.certificate}")
    if is_wget2 and options.certificate_type:
        args.append(f"--certificate-type={options.certificate_type.value}")
    if options.private_key:
        args.append(f"--private-key={options.private_key}")
    if is_wget2 and options.private_key_type:
        args.append(f"--private-key-type={options.private_key_type.value}")
    if is_wget2 and options.crl_file:
        args.append(f"--crl-file={options.crl_file}")
    if options.secure_protocol:
        args.append(f"--secure-protocol={options.secure_protocol}")
    if is_wget2:
        _append_bool_arg(args, options.ocsp, "--ocsp", "--no-ocsp")
        _append_bool_arg(args, options.ocsp_date, "--ocsp-date", "--no-ocsp-date")
        if options.ocsp_file:
            args.append(f"--ocsp-file={options.ocsp_file}")
        _append_bool_arg(args, options.ocsp_nonce, "--ocsp-nonce", "--no-ocsp-nonce")
        if options.ocsp_server:
            args.append(f"--ocsp-server={options.ocsp_server}")
        _append_bool_arg(
            args,
            options.ocsp_stapling,
            "--ocsp-stapling",
            "--no-ocsp-stapling",
        )
        _append_bool_arg(
            args,
            options.tls_false_start,
            "--tls-false-start",
            "--no-tls-false-start",
        )
        _append_bool_arg(args, options.tls_resume, "--tls-resume", "--no-tls-resume")
        if options.tls_session_file:
            args.append(f"--tls-session-file={options.tls_session_file}")
        _append_bool_arg(args, options.http2, "--http2", "--no-http2")
        if options.http2_only:
            args.append("--http2-only")
        if options.http2_request_window is not None:
            args.append(f"--http2-request-window={options.http2_request_window}")
    if options.content_on_error:
        args.append("--content-on-error")
    if is_wget2 and options.save_content_on:
        args.append(f"--save-content-on={options.save_content_on}")
    if options.save_headers:
        args.append("--save-headers")
    if options.server_response:
        args.append("--server-response")
    if options.ignore_length:
        args.append("--ignore-length")
    if is_wget2:
        if options.verify_sig:
            args.append(f"--verify-sig={options.verify_sig.value}")
        if options.signature_extensions:
            args.append(f"--signature-extensions={options.signature_extensions}")
        if options.gnupg_homedir:
            args.append(f"--gnupg-homedir={options.gnupg_homedir}")
        if options.verify_save_failed:
            args.append("--verify-save-failed")
    quota = _mirror_quota(options)
    if quota:
        args.append(f"--quota={quota}")
    if options.limit_rate:
        args.append(f"--limit-rate={options.limit_rate}")
    if options.retry_connrefused:
        args.append("--retry-connrefused")
    if is_wget2 and options.start_pos:
        args.append(f"--start-pos={options.start_pos}")
    return args


def _append_bool_arg(
    args: list[str],
    value: bool | None,
    enabled: str,
    disabled: str,
) -> None:
    if value is None:
        return
    args.append(enabled if value else disabled)


def _mirror_hub_kind(options: SiteDownloadOptions) -> HubKind:
    return HubKind.dir if isinstance(options, DirectoryMirrorOptions) else HubKind.site


def _mirror_safety_warnings(options: SiteDownloadOptions) -> list[str]:
    if isinstance(options, DirectoryMirrorOptions):
        warnings = [
            "open HTTP directory mirroring can download large public file trees",
            "directory mirroring is bounded by depth and no-parent defaults",
        ]
    else:
        warnings = []
    if options.span_hosts:
        warnings.append("host spanning is enabled; this can expand the mirror scope")
    if options.depth >= 5:
        warnings.append("high recursive depth can create a large mirror")
    if options.max_files is not None:
        warnings.append(
            "max-files is enforced during Atlas scan planning when scan counts are available"
        )
    if options.max_runtime is not None:
        warnings.append(f"mirror subprocess runtime is capped at {options.max_runtime:g} seconds")
    return warnings


def _mirror_quota(options: SiteDownloadOptions) -> str | None:
    if options.max_total_size and options.quota and options.max_total_size != options.quota:
        raise EngineError("Use either --max-total-size or --quota, not both.")
    return options.max_total_size or options.quota


def _wget2_capability_warnings(options: SiteDownloadOptions) -> list[str]:
    try:
        from atlas.doctor import _wget2_capabilities
    except ImportError:
        return []
    capabilities = _wget2_capabilities()
    if capabilities is None:
        return []
    features = capabilities.features
    warnings: list[str] = []
    if (
        options.http2 is True or options.http2_only or options.http2_request_window is not None
    ) and not features.get("http2", False):
        warnings.append("selected HTTP/2 options, but this wget2 build lacks +http2")
    compression = (options.compression or "").lower()
    if "br" in compression and not features.get("brotli", False):
        warnings.append("selected Brotli compression, but this wget2 build lacks +brotli")
    if "zstd" in compression and not features.get("zstd", False):
        warnings.append("selected zstd compression, but this wget2 build lacks +zstd")
    if (options.https_only or options.https_enforce or _site_uses_tls_security(options)) and not (
        features.get("https", False) or features.get("ssl", False)
    ):
        warnings.append(
            "selected HTTPS policy options, but this wget2 build lacks HTTPS/SSL support"
        )
    if (options.hsts is True or options.hsts_file) and not features.get("hsts", False):
        warnings.append("selected HSTS persistence, but this wget2 build lacks +hsts")
    if _site_uses_cookie_store(options) and not features.get("psl", False):
        warnings.append(
            "selected cookie store options, but this wget2 build lacks +psl; "
            "cookie domain matching may be weaker"
        )
    if options.force_metalink and not features.get("gpgme", False):
        warnings.append(
            "selected Metalink parser options, but this wget2 build lacks +gpgme; "
            "signed Metalinks cannot be verified"
        )
    if options.verify_sig and not features.get("gpgme", False):
        warnings.append(
            "selected signature verification, but this wget2 build lacks +gpgme; "
            "detached signatures cannot be verified"
        )
    if (
        options.inet6_only or (options.prefer_family and options.prefer_family.value == "IPv6")
    ) and not features.get("ipv6", False):
        warnings.append("selected IPv6 options, but this wget2 build lacks +ipv6")
    if _site_uses_idn(options) and not features.get("idn2", False):
        warnings.append("selected internationalized hostnames, but this wget2 build lacks +idn2")
    return warnings


def _site_uses_cookie_store(options: SiteDownloadOptions) -> bool:
    return bool(
        options.cookies is not None
        or options.browser_cookies
        or options.load_cookies
        or options.save_cookies
        or options.keep_session_cookies
        or options.cookie_suffixes
    )


def _site_uses_tls_security(options: SiteDownloadOptions) -> bool:
    return bool(
        options.check_certificate is not None
        or options.check_hostname is not None
        or options.ca_certificate
        or options.ca_directory
        or options.certificate
        or options.certificate_type
        or options.private_key
        or options.private_key_type
        or options.crl_file
        or options.secure_protocol
        or options.ocsp is not None
        or options.ocsp_date is not None
        or options.ocsp_file
        or options.ocsp_nonce is not None
        or options.ocsp_server
        or options.ocsp_stapling is not None
        or options.tls_false_start is not None
        or options.tls_resume is not None
        or options.tls_session_file
    )


def _site_uses_idn(options: SiteDownloadOptions) -> bool:
    values = [
        options.url,
        options.base or "",
        options.domains or "",
        options.exclude_domains or "",
    ]
    return any(any(ord(char) > 127 for char in value) for value in values)


def _export_browser_cookies_to_file(browser_cookies: str, directory: Path) -> Path:
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        from atlas.presets import _cookies_from_browser
    except ImportError as exc:
        raise EngineError(
            "Browser cookie export requires yt-dlp's browser cookie support."
        ) from exc
    try:
        spec = _cookies_from_browser(browser_cookies)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc
    if spec is None:
        raise EngineError("--cookies-from-browser requires a browser selector")
    browser_name, profile, keyring, container = spec
    try:
        cookie_jar = extract_cookies_from_browser(
            browser_name,
            profile,
            keyring=keyring,
            container=container,
        )
    except Exception as exc:  # pragma: no cover - browser/keychain failures are host-specific.
        raise EngineError(f"Could not export {browser_name} cookies: {exc}") from exc
    path = directory / "browser-cookies.txt"
    cookie_jar.save(str(path), ignore_discard=True, ignore_expires=True)
    return path


def _aria2_checksum_algorithm(algorithm: str) -> str:
    if algorithm == "sha256":
        return "sha-256"
    if algorithm == "sha512":
        return "sha-512"
    return algorithm


def _require_native_resume_support(path: Path, probe: object | None) -> None:
    if not probe or not getattr(probe, "probed", False):
        raise EngineError(
            f"Cannot resume {path}: remote byte-range support has not been confirmed. "
            "Remove the partial file, pass --overwrite, or use aria2c."
        )
    if not getattr(probe, "supports_ranges", False):
        raise EngineError(
            f"Cannot resume {path}: server did not advertise byte-range support. "
            "Remove the partial file or pass --overwrite."
        )


def _local_file_is_current(path: Path, probe: object | None) -> bool:
    if not probe or not getattr(probe, "probed", False):
        return False
    remote_size = getattr(probe, "content_length", None)
    if remote_size is not None and path.stat().st_size != remote_size:
        return False
    last_modified = getattr(probe, "last_modified", None)
    remote_mtime = _http_timestamp(last_modified)
    if remote_mtime is None:
        return False
    return path.stat().st_mtime >= remote_mtime


def _http_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _apply_server_timestamp(path: Path, last_modified: str | None) -> None:
    timestamp = _http_timestamp(last_modified)
    if timestamp is None:
        return
    os.utime(path, (timestamp, timestamp))


def _response_status(response: object) -> int | None:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        code = getcode()
        if isinstance(code, int):
            return code
    return None


def _response_url(response: object) -> str | None:
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return None
    value = geturl()
    return value if isinstance(value, str) else None


def _assert_native_download_complete(path: Path, *, completed: int, total: int | None) -> None:
    if total is None:
        return
    if completed != total:
        raise EngineError(
            f"Downloaded size mismatch for {path.name}: expected {total} bytes, got {completed}"
        )


def _emit_native_skip(
    callback: ProgressCallback | None,
    options: FileDownloadOptions,
    plan: BackendPlan,
    *,
    message: str,
) -> None:
    downloaded = plan.output.stat().st_size if plan.output.exists() else 0
    _emit(
        callback,
        ProgressEvent(
            engine=EngineKind.native,
            status="skipped",
            phase=ProgressPhase.done,
            kind=HubKind.file,
            filename=plan.output.name,
            url=options.url,
            downloaded_bytes=downloaded,
            total_bytes=downloaded,
            message=message,
        ),
    )


def _wget2_stats_files(stats_dir: Path) -> dict[str, Path]:
    return {
        "site": stats_dir / "site.csv",
        "server": stats_dir / "server.csv",
        "dns": stats_dir / "dns.csv",
        "tls": stats_dir / "tls.csv",
        "ocsp": stats_dir / "ocsp.csv",
    }


def _wget2_stats_args(files: Mapping[str, Path]) -> list[str]:
    return [
        f"--stats-site=csv:{files['site']}",
        f"--stats-server=csv:{files['server']}",
        f"--stats-dns=csv:{files['dns']}",
        f"--stats-tls=csv:{files['tls']}",
        f"--stats-ocsp=csv:{files['ocsp']}",
    ]


def _parse_wget2_stats_file(path: Path, *, label: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"format": "empty", "rows": []}
    sample = "\n".join(lines[:2])
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(lines, dialect=dialect)
    raw_rows = [row for row in reader if row]
    if not raw_rows:
        return {"format": "empty", "rows": []}
    known_headers = _known_wget2_stats_headers(label, raw_rows[0])
    if known_headers:
        rows = [_row_from_headers(known_headers, row) for row in raw_rows]
        return {"format": "csv", "rows": rows}
    if _looks_like_csv_header(raw_rows[0]):
        rows = list(csv.DictReader(lines, dialect=dialect))
    else:
        return {"format": "text", "lines": lines}
    if rows and any(row.keys() for row in rows):
        return {"format": "csv", "rows": rows}
    return {"format": "text", "lines": lines}


def _known_wget2_stats_headers(label: str, first_row: list[str]) -> list[str] | None:
    if label == "site" and len(first_row) == 14 and _is_int_like(first_row[0]):
        return [
            "id",
            "parent_id",
            "url",
            "status",
            "not_redirect",
            "method",
            "downloaded_bytes",
            "decompressed_bytes",
            "transfer_time_ms",
            "response_time_ms",
            "encoding",
            "signature_status",
            "last_modified",
            "mime_type",
        ]
    server_headers = ["hostname", "ip", "scheme", "hpkp", "hpkp_new", "hsts", "csp"]
    if label == "server" and len(first_row) == len(server_headers):
        return None if _row_matches_headers(first_row, server_headers) else server_headers
    dns_headers = ["hostname", "ip", "port", "dns_secs"]
    if label == "dns" and len(first_row) == len(dns_headers):
        return None if _row_matches_headers(first_row, dns_headers) else dns_headers
    tls_headers = [
        "hostname",
        "version",
        "false_start",
        "tfo",
        "resumed",
        "alpn_protocol",
        "http_protocol",
        "cert_chain_size",
        "tls_secs",
    ]
    if label == "tls" and len(first_row) == len(tls_headers):
        return None if _row_matches_headers(first_row, tls_headers) else tls_headers
    ocsp_headers = ["hostname", "stapling", "nvalid", "nrevoked", "nignored"]
    if label == "ocsp" and len(first_row) == len(ocsp_headers):
        return None if _row_matches_headers(first_row, ocsp_headers) else ocsp_headers
    return None


def _row_matches_headers(row: list[str], headers: list[str]) -> bool:
    return [cell.strip().lower().replace("-", "_").replace(" ", "_") for cell in row] == [
        header.lower().replace("-", "_").replace(" ", "_") for header in headers
    ]


def _looks_like_csv_header(row: list[str]) -> bool:
    return any(not _is_int_like(cell) for cell in row[:1]) and all(cell.strip() for cell in row)


def _row_from_headers(headers: list[str], row: list[str]) -> dict[str, str]:
    padded = [*row, *([""] * max(len(headers) - len(row), 0))]
    return dict(zip(headers, padded, strict=False))


def _summarize_wget2_stats(parsed: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    site_rows = _stats_rows(parsed.get("site"))
    if site_rows:
        summary["site"] = _summarize_site_stats(site_rows)
    server_rows = _stats_rows(parsed.get("server"))
    if server_rows:
        summary["server"] = _summarize_server_stats(server_rows)
    dns_rows = _stats_rows(parsed.get("dns"))
    if dns_rows:
        summary["dns"] = _summarize_dns_stats(dns_rows)
    tls_rows = _stats_rows(parsed.get("tls"))
    if tls_rows:
        summary["tls"] = _summarize_tls_stats(tls_rows)
    ocsp_rows = _stats_rows(parsed.get("ocsp"))
    if ocsp_rows:
        summary["ocsp"] = _summarize_ocsp_stats(ocsp_rows)
    return summary


def _stats_rows(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append({str(key): str(value) for key, value in row.items()})
    return normalized


def _summarize_site_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    mime_types: dict[str, int] = {}
    downloaded_bytes = 0
    decompressed_bytes = 0
    transfer_time_ms = 0
    response_time_ms = 0
    redirects = 0
    failures = 0
    for row in rows:
        status = _row_lookup(row, "status", "Status")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
            status_int = _int_or_none(status)
            if status_int is not None:
                if 300 <= status_int < 400:
                    redirects += 1
                if status_int >= 400:
                    failures += 1
        not_redirect = _int_or_none(_row_lookup(row, "not_redirect"))
        if not_redirect == 0:
            redirects += 1
        mime_type = _row_lookup(row, "mime_type", "mime", "content_type", "Content-Type")
        if mime_type:
            mime_types[mime_type] = mime_types.get(mime_type, 0) + 1
        downloaded_bytes += (
            _int_or_none(
                _row_lookup(
                    row,
                    "downloaded_bytes",
                    "size_downloaded",
                    "downloaded",
                    "Size",
                )
            )
            or 0
        )
        decompressed_bytes += (
            _int_or_none(
                _row_lookup(
                    row,
                    "decompressed_bytes",
                    "size_decompressed",
                    "decompressed",
                    "SizeDecompressed",
                )
            )
            or 0
        )
        transfer_time_ms += (
            _int_or_none(
                _row_lookup(row, "transfer_time_ms", "transfer_time", "TransferTime", "ms")
            )
            or 0
        )
        response_time_ms += (
            _int_or_none(
                _row_lookup(row, "response_time_ms", "ResponseTime", "initial_response_duration")
            )
            or 0
        )
    return {
        "urls": len(rows),
        "status_counts": status_counts,
        "failures": failures,
        "redirects": redirects,
        "downloaded_bytes": downloaded_bytes,
        "decompressed_bytes": decompressed_bytes,
        "transfer_time_ms": transfer_time_ms,
        "response_time_ms": response_time_ms,
        "mime_types": mime_types,
    }


def _summarize_server_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    schemes: dict[str, int] = {}
    hosts: set[str] = set()
    hsts_hosts: set[str] = set()
    csp_hosts: set[str] = set()
    host_schemes: dict[str, set[str]] = {}
    for index, row in enumerate(rows):
        hostname = _row_lookup(row, "hostname", "host") or f"row:{index}"
        hosts.add(hostname)
        scheme = _row_lookup(row, "scheme")
        if scheme:
            schemes[scheme] = schemes.get(scheme, 0) + 1
            host_schemes.setdefault(hostname, set()).add(scheme.lower())
        if _truthy_stats_value(_row_lookup(row, "hsts")):
            hsts_hosts.add(hostname)
        if _truthy_stats_value(_row_lookup(row, "csp")):
            csp_hosts.add(hostname)
    https_hosts = {host for host, host_scheme in host_schemes.items() if "https" in host_scheme}
    http_hosts = {host for host, host_scheme in host_schemes.items() if "http" in host_scheme}
    mixed_scheme_hosts = sorted(https_hosts & http_hosts)
    return {
        "hosts": len(hosts),
        "schemes": schemes,
        "hsts_hosts": len(hsts_hosts),
        "csp_hosts": len(csp_hosts),
        "https_hosts": len(https_hosts),
        "http_hosts": len(http_hosts),
        "hosts_without_hsts": sorted(https_hosts - hsts_hosts),
        "hosts_without_csp": sorted(hosts - csp_hosts),
        "mixed_scheme_hosts": mixed_scheme_hosts,
    }


def _summarize_dns_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    hosts: set[str] = set()
    addresses: set[str] = set()
    ports: set[int] = set()
    lookup_time_ms = 0
    max_lookup_time_ms = 0
    failures = 0
    for row in rows:
        hostname = _row_lookup(row, "hostname", "host")
        if hostname:
            hosts.add(hostname)
        ip_address = _row_lookup(row, "ip", "address")
        if ip_address:
            addresses.add(ip_address)
        else:
            failures += 1
        port = _int_or_none(_row_lookup(row, "port"))
        if port is not None:
            ports.add(port)
        lookup = _int_or_none(_row_lookup(row, "dns_secs", "dns_ms", "lookup_time_ms")) or 0
        lookup_time_ms += lookup
        max_lookup_time_ms = max(max_lookup_time_ms, lookup)
    return {
        "lookups": len(rows),
        "hosts": len(hosts),
        "addresses": len(addresses),
        "ipv4_addresses": len([address for address in addresses if ":" not in address]),
        "ipv6_addresses": len([address for address in addresses if ":" in address]),
        "ports": sorted(ports),
        "failures": failures,
        "lookup_time_ms": lookup_time_ms,
        "max_lookup_time_ms": max_lookup_time_ms,
        "average_lookup_time_ms": _average_int(lookup_time_ms, len(rows)),
    }


def _summarize_tls_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    versions: dict[str, int] = {}
    alpn_protocols: dict[str, int] = {}
    http_protocols: dict[str, int] = {}
    tls_time_ms = 0
    max_tls_time_ms = 0
    max_cert_chain_size = 0
    false_start_connections = 0
    tfo_connections = 0
    resumed_connections = 0
    for row in rows:
        version = _tls_version_name(_row_lookup(row, "version"))
        if version:
            versions[version] = versions.get(version, 0) + 1
        if _truthy_stats_value(_row_lookup(row, "false_start")):
            false_start_connections += 1
        if _truthy_stats_value(_row_lookup(row, "tfo")):
            tfo_connections += 1
        if _truthy_stats_value(_row_lookup(row, "resumed")):
            resumed_connections += 1
        alpn = _row_lookup(row, "alpn_protocol", "alpn")
        if alpn:
            alpn_protocols[alpn] = alpn_protocols.get(alpn, 0) + 1
        http_protocol = _http_protocol_name(_row_lookup(row, "http_protocol"))
        if http_protocol:
            http_protocols[http_protocol] = http_protocols.get(http_protocol, 0) + 1
        cert_chain_size = _int_or_none(_row_lookup(row, "cert_chain_size")) or 0
        max_cert_chain_size = max(max_cert_chain_size, cert_chain_size)
        tls_time = _int_or_none(_row_lookup(row, "tls_secs", "tls_ms", "tls_time_ms")) or 0
        tls_time_ms += tls_time
        max_tls_time_ms = max(max_tls_time_ms, tls_time)
    return {
        "connections": len(rows),
        "versions": versions,
        "false_start_connections": false_start_connections,
        "tfo_connections": tfo_connections,
        "resumed_connections": resumed_connections,
        "alpn_protocols": alpn_protocols,
        "http_protocols": http_protocols,
        "max_cert_chain_size": max_cert_chain_size,
        "tls_time_ms": tls_time_ms,
        "max_tls_time_ms": max_tls_time_ms,
        "average_tls_time_ms": _average_int(tls_time_ms, len(rows)),
    }


def _summarize_ocsp_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    hosts: set[str] = set()
    stapled_hosts: set[str] = set()
    revoked_hosts: set[str] = set()
    ignored_hosts: set[str] = set()
    valid_responses = 0
    revoked_responses = 0
    ignored_responses = 0
    for index, row in enumerate(rows):
        hostname = _row_lookup(row, "hostname", "host") or f"row:{index}"
        hosts.add(hostname)
        if _truthy_stats_value(_row_lookup(row, "stapling")):
            stapled_hosts.add(hostname)
        valid = _int_or_none(_row_lookup(row, "nvalid", "valid")) or 0
        revoked = _int_or_none(_row_lookup(row, "nrevoked", "revoked")) or 0
        ignored = _int_or_none(_row_lookup(row, "nignored", "ignored")) or 0
        valid_responses += valid
        revoked_responses += revoked
        ignored_responses += ignored
        if revoked:
            revoked_hosts.add(hostname)
        if ignored:
            ignored_hosts.add(hostname)
    return {
        "hosts": len(hosts),
        "stapled_hosts": len(stapled_hosts),
        "valid_responses": valid_responses,
        "revoked_responses": revoked_responses,
        "ignored_responses": ignored_responses,
        "revoked_hosts": sorted(revoked_hosts),
        "ignored_hosts": sorted(ignored_hosts),
    }


def _average_int(total: int, count: int) -> int:
    if count <= 0:
        return 0
    return round(total / count)


def _tls_version_name(value: str) -> str:
    version = _int_or_none(value)
    if version is None:
        return value.strip()
    return {
        1: "SSL3",
        2: "TLS1.0",
        3: "TLS1.1",
        4: "TLS1.2",
        5: "TLS1.3",
    }.get(version, f"unknown:{version}")


def _http_protocol_name(value: str) -> str:
    protocol = _int_or_none(value)
    if protocol is None:
        return value.strip()
    return {
        1: "HTTP/1.1",
        2: "HTTP/2",
    }.get(protocol, f"unknown:{protocol}")


def _row_lookup(row: Mapping[str, str], *names: str) -> str:
    casefold = {
        key.lower().replace("-", "_").replace(" ", "_"): value for key, value in row.items()
    }
    for name in names:
        value = casefold.get(name.lower().replace("-", "_").replace(" ", "_"))
        if value:
            return value
    return ""


def _truthy_stats_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_or_none(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _is_int_like(value: str) -> bool:
    return _int_or_none(value) is not None


def _emit(callback: ProgressCallback | None, event: ProgressEvent | None) -> None:
    if callback and event:
        callback(event)
