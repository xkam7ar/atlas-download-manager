"""Structured aria2 JSON-RPC adapter for direct-file downloads."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import time
from base64 import b64encode
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from atlas.adaptive import AdaptiveScheduler
from atlas.errors import EngineError
from atlas.models import (
    EngineKind,
    FileDownloadOptions,
    HubKind,
    ProgressEvent,
    ProgressPhase,
)
from atlas.network import open_request, redirect_safe_request
from atlas.private_files import prepare_private_file
from atlas.urls import is_metalink_url

ProgressCallback = Callable[[ProgressEvent], None]


class Aria2RpcError(EngineError):
    """Raised when aria2 JSON-RPC returns an error object."""


class Aria2RpcStartupError(EngineError):
    """Raised when a local aria2 RPC subprocess cannot be started or reached."""


class Aria2RpcClientProtocol(Protocol):
    def call(self, method: str, params: Sequence[object]) -> object: ...


class ProcessProtocol(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


PopenFactory = Callable[..., ProcessProtocol]
RpcClientFactory = Callable[[str, float], Aria2RpcClientProtocol]
PortFactory = Callable[[], int]
TokenFactory = Callable[[], str]
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]
_BACKOFF_STATUS_PATTERN = re.compile(r"\b(403|429|503)\b")
_MAX_METALINK_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Aria2RpcDownloadResult:
    gid: str
    output: Path
    status: Mapping[str, object]


@dataclass(frozen=True)
class Aria2RpcQueuedDownload:
    options: FileDownloadOptions
    output: Path
    progress_callback: ProgressCallback | None = None


@dataclass(frozen=True)
class Aria2RpcQueuedDownloadResult:
    item: Aria2RpcQueuedDownload
    result: Aria2RpcDownloadResult | None = None
    error: str | None = None


class Aria2RpcClient:
    """Tiny JSON-RPC-over-HTTP client for a localhost aria2c instance."""

    def __init__(self, endpoint: str, timeout: float = 2.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._next_id = 0
        # The RPC secret and queued download URLs must never be handed to an
        # ambient HTTP(S)_PROXY.  This client only talks to the loopback aria2
        # process, so bypass proxies explicitly rather than relying on NO_PROXY.
        self._opener = build_opener(ProxyHandler({}))

    def call(self, method: str, params: Sequence[object]) -> object:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": f"atlas-{self._next_id}",
            "method": method,
            "params": list(params),
        }
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            self._endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(request, timeout=self._timeout) as response:
            raw = response.read().decode("utf-8")
        decoded = json.loads(raw)
        error = decoded.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or "aria2 RPC error"
            raise Aria2RpcError(f"aria2 RPC {code}: {message}")
        return decoded.get("result")


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _new_rpc_secret() -> str:
    return secrets.token_urlsafe(32)


class Aria2RpcSession:
    """Own a short-lived localhost-only aria2c RPC subprocess."""

    def __init__(
        self,
        *,
        executable: str = "aria2c",
        progress_interval: float = 0.5,
        startup_timeout: float = 5.0,
        request_timeout: float = 2.0,
        max_concurrent_downloads: int | None = None,
        input_file: Path | None = None,
        save_session: Path | None = None,
        save_session_interval: int | None = None,
        server_stat_if: Path | None = None,
        server_stat_of: Path | None = None,
        server_stat_timeout: int | None = None,
        uri_selector: str | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        rpc_client_factory: RpcClientFactory = Aria2RpcClient,
        port_factory: PortFactory = _allocate_local_port,
        token_factory: TokenFactory = _new_rpc_secret,
        sleep: SleepFn = time.sleep,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self.executable = executable
        self.progress_interval = progress_interval
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.max_concurrent_downloads = max_concurrent_downloads
        self.input_file = input_file
        self.save_session = save_session
        self.save_session_interval = save_session_interval
        self.server_stat_if = server_stat_if
        self.server_stat_of = server_stat_of
        self.server_stat_timeout = server_stat_timeout
        self.uri_selector = uri_selector
        self._popen_factory = popen_factory
        self._rpc_client_factory = rpc_client_factory
        self._port_factory = port_factory
        self._token_factory = token_factory
        self._sleep = sleep
        self._clock = clock
        self._process: ProcessProtocol | None = None
        self._client: Aria2RpcClientProtocol | None = None
        self._secret: str | None = None
        self._endpoint: str | None = None
        self._last_adaptive_options: dict[str, str] | None = None

    @staticmethod
    def redacted_command(
        executable: str = "aria2c",
        *,
        input_file: Path | None = None,
        save_session: Path | None = None,
        save_session_interval: int | None = None,
        max_concurrent_downloads: int | None = None,
        server_stat_if: Path | None = None,
        server_stat_of: Path | None = None,
        server_stat_timeout: int | None = None,
        uri_selector: str | None = None,
    ) -> list[str]:
        command = [
            executable,
            "--no-conf=true",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-listen-port=<ephemeral>",
            "--rpc-secret=<redacted>",
            "--rpc-allow-origin-all=false",
            "--console-log-level=warn",
            "--summary-interval=0",
            "--show-console-readout=false",
            "--download-result=hide",
            "--log=",
        ]
        if max_concurrent_downloads is not None:
            command.append(f"--max-concurrent-downloads={max_concurrent_downloads}")
        if input_file:
            command.append(f"--input-file={input_file}")
        if save_session:
            command.append(f"--save-session={save_session}")
            command.append("--force-save=true")
        if save_session_interval is not None:
            command.append(f"--save-session-interval={save_session_interval}")
        if server_stat_if:
            command.append(f"--server-stat-if={server_stat_if}")
        if server_stat_of:
            command.append(f"--server-stat-of={server_stat_of}")
        if server_stat_timeout is not None:
            command.append(f"--server-stat-timeout={server_stat_timeout}")
        if uri_selector:
            command.append(f"--uri-selector={uri_selector}")
        return command

    def download(
        self,
        options: FileDownloadOptions,
        output: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Aria2RpcDownloadResult:
        results = self.download_many(
            [
                Aria2RpcQueuedDownload(
                    options=options,
                    output=output,
                    progress_callback=progress_callback,
                )
            ]
        )
        queued_result = results[0]
        if queued_result.error or queued_result.result is None:
            raise EngineError(queued_result.error or "aria2c download failed")
        return queued_result.result

    def download_many(
        self,
        downloads: Sequence[Aria2RpcQueuedDownload],
        *,
        adaptive_scheduler: AdaptiveScheduler | None = None,
    ) -> list[Aria2RpcQueuedDownloadResult]:
        if not downloads:
            return []
        self._inherit_global_options(downloads[0].options)
        self.start()
        if adaptive_scheduler is not None:
            self._apply_adaptive_options(adaptive_scheduler)
        results: list[Aria2RpcQueuedDownloadResult | None] = [None] * len(downloads)
        gids_by_index: dict[int, list[str]] = {}
        active_gids: list[str] = []
        pending: set[int] = set()
        try:
            for index, item in enumerate(downloads):
                item.output.parent.mkdir(parents=True, exist_ok=True)
                _emit(
                    item.progress_callback,
                    _with_adaptive_aria2_context(
                        ProgressEvent(
                            engine=EngineKind.aria2,
                            phase=ProgressPhase.download,
                            kind=_aria2_progress_kind(item.options),
                            status="starting",
                            filename=item.output.name,
                            url=item.options.url,
                            message="starting aria2c RPC",
                        ),
                        item.options,
                        adaptive_scheduler,
                    ),
                )
                try:
                    gids = self._add_download(item.options, item.output)
                except Exception as exc:
                    message = str(exc)
                    _emit_queue_error(item, message)
                    results[index] = Aria2RpcQueuedDownloadResult(item=item, error=message)
                    continue
                gids_by_index[index] = gids
                active_gids.extend(gids)

            pending = set(gids_by_index)
            while pending:
                completed_this_tick = False
                for index in list(pending):
                    item = downloads[index]
                    gids = gids_by_index[index]
                    status = _aggregate_statuses(
                        [self._tell_status(active_gid) for active_gid in gids]
                    )
                    event = progress_event_from_aria2_rpc_status(
                        status,
                        filename=item.output.name,
                        url=item.options.url,
                        kind=_aria2_progress_kind(item.options),
                    )
                    rpc_status = _status_text(status)
                    if adaptive_scheduler is not None:
                        if rpc_status in {"complete", "error", "removed"}:
                            _record_adaptive_aria2_status(
                                adaptive_scheduler,
                                status,
                                rpc_status,
                                host=_host_from_url(item.options.url),
                            )
                        else:
                            adaptive_scheduler.observe_progress_event(event)
                        self._apply_adaptive_options(adaptive_scheduler)
                    event = _with_adaptive_aria2_context(
                        event,
                        item.options,
                        adaptive_scheduler,
                    )
                    if rpc_status == "complete":
                        results[index] = _queued_success_result(item, gids, status)
                        pending.remove(index)
                        completed_this_tick = True
                    elif rpc_status in {"error", "removed"}:
                        message = _aria2_failure_message(status, rpc_status)
                        _emit_queue_error(item, message, status=status)
                        results[index] = Aria2RpcQueuedDownloadResult(
                            item=item,
                            error=message,
                        )
                        pending.remove(index)
                        completed_this_tick = True
                    else:
                        _emit(item.progress_callback, event)
                if pending and not completed_this_tick:
                    self._sleep(self.progress_interval)
            return [
                result
                if result is not None
                else Aria2RpcQueuedDownloadResult(
                    item=downloads[index],
                    error="aria2c download was not queued",
                )
                for index, result in enumerate(results)
            ]
        except (EngineError, OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
            message = str(exc) or type(exc).__name__
            unresolved = pending or {index for index in gids_by_index if results[index] is None}
            for index in unresolved:
                item = downloads[index]
                _emit_queue_error(item, message)
                results[index] = Aria2RpcQueuedDownloadResult(item=item, error=message)
            for active_gid in active_gids:
                self._try_remove(active_gid)
            return [
                result
                if result is not None
                else Aria2RpcQueuedDownloadResult(
                    item=downloads[index],
                    error=message,
                )
                for index, result in enumerate(results)
            ]
        except BaseException:
            for active_gid in active_gids:
                self._try_remove(active_gid)
            raise
        finally:
            self.close()

    def start(self) -> None:
        if self._process is not None:
            return
        self._last_adaptive_options = None
        if self.save_session is not None:
            try:
                prepare_private_file(self.save_session)
            except OSError as exc:
                raise Aria2RpcStartupError(
                    f"Could not prepare private aria2 session file {self.save_session}: {exc}"
                ) from exc
        port = self._port_factory()
        secret = self._token_factory()
        endpoint = f"http://127.0.0.1:{port}/jsonrpc"
        command = self._command(port=port, secret=secret)
        try:
            process = self._popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except OSError as exc:
            raise Aria2RpcStartupError(f"Could not start aria2c RPC: {exc}") from exc
        self._process = process
        self._secret = secret
        self._endpoint = endpoint
        try:
            self._client = self._rpc_client_factory(endpoint, self.request_timeout)
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        session_hardening_error: OSError | None = None
        try:
            if process.poll() is None:
                if self.save_session:
                    with suppress(Exception):
                        self._rpc("aria2.saveSession")
                with suppress(Exception):
                    self._rpc("aria2.shutdown")
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)
        finally:
            if self.save_session is not None and os.path.lexists(self.save_session):
                try:
                    prepare_private_file(self.save_session)
                except OSError as exc:
                    session_hardening_error = exc
            self._process = None
            self._client = None
            self._secret = None
            self._endpoint = None
            self._last_adaptive_options = None
        if session_hardening_error is not None:
            raise Aria2RpcError(
                f"Could not harden private aria2 session file {self.save_session}: "
                f"{session_hardening_error}"
            ) from session_hardening_error

    def _command(self, *, port: int, secret: str) -> list[str]:
        command = self.redacted_command(
            self.executable,
            input_file=self.input_file,
            save_session=self.save_session,
            save_session_interval=self.save_session_interval,
            max_concurrent_downloads=self.max_concurrent_downloads,
            server_stat_if=self.server_stat_if,
            server_stat_of=self.server_stat_of,
            server_stat_timeout=self.server_stat_timeout,
            uri_selector=self.uri_selector,
        )
        resolved = [
            arg.replace("<ephemeral>", str(port)).replace("<redacted>", secret) for arg in command
        ]
        return resolved

    def _inherit_global_options(self, options: FileDownloadOptions) -> None:
        if self.input_file is None:
            self.input_file = options.input_file
        if self.save_session is None:
            self.save_session = options.save_session
        if self.save_session_interval is None:
            self.save_session_interval = options.save_session_interval
        if self.server_stat_if is None:
            self.server_stat_if = options.server_stat_if
        if self.server_stat_of is None:
            self.server_stat_of = options.server_stat_of
        if self.server_stat_timeout is None:
            self.server_stat_timeout = options.server_stat_timeout
        if self.uri_selector is None and options.uri_selector:
            self.uri_selector = options.uri_selector.value

    def _wait_until_ready(self) -> None:
        deadline = self._clock() + self.startup_timeout
        last_error: BaseException | None = None
        while self._clock() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                raise Aria2RpcStartupError("aria2c RPC process exited during startup")
            try:
                self._rpc("aria2.getVersion")
                return
            except (Aria2RpcError, OSError, TimeoutError, URLError) as exc:
                last_error = exc
                self._sleep(0.05)
        message = f": {last_error}" if last_error else ""
        raise Aria2RpcStartupError(f"aria2c RPC did not become ready{message}")

    def _add_download(self, options: FileDownloadOptions, output: Path) -> list[str]:
        if _is_metalink_download(options):
            return self._add_metalink(options, output)
        return [self._add_uri(options, output)]

    def _add_uri(self, options: FileDownloadOptions, output: Path) -> str:
        result = self._rpc(
            "aria2.addUri",
            [options.url],
            _rpc_download_options(options, output),
        )
        if not isinstance(result, str) or not result:
            raise EngineError("aria2.addUri did not return a download id")
        return result

    def _add_metalink(self, options: FileDownloadOptions, output: Path) -> list[str]:
        manifest = b64encode(_fetch_metalink(options)).decode("ascii")
        result = self._rpc(
            "aria2.addMetalink",
            manifest,
            _rpc_download_options(options, output, metalink=True),
        )
        if not isinstance(result, list) or not result:
            raise EngineError("aria2.addMetalink did not return download ids")
        gids = [gid for gid in result if isinstance(gid, str) and gid]
        if not gids:
            raise EngineError("aria2.addMetalink returned no usable download ids")
        return gids

    def _tell_status(self, gid: str) -> Mapping[str, object]:
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "connections",
            "files",
            "errorCode",
            "errorMessage",
            "verifiedLength",
            "verifyIntegrityPending",
            "pieceLength",
            "numPieces",
            "bitfield",
            "followedBy",
            "following",
            "belongsTo",
        ]
        result = self._rpc("aria2.tellStatus", gid, keys)
        if not isinstance(result, dict):
            raise EngineError("aria2.tellStatus returned unexpected data")
        return result

    def _try_remove(self, gid: str) -> None:
        with suppress(Exception):
            self._rpc("aria2.remove", gid)

    def _apply_adaptive_options(self, scheduler: AdaptiveScheduler) -> None:
        options = {"max-concurrent-downloads": str(max(1, scheduler.current_concurrency))}
        if scheduler.current_speed_limit:
            options["max-overall-download-limit"] = scheduler.current_speed_limit
        if options == self._last_adaptive_options:
            return
        self._rpc("aria2.changeGlobalOption", options)
        self._last_adaptive_options = options

    def _rpc(self, method: str, *params: object) -> object:
        if self._client is None or self._secret is None:
            raise Aria2RpcStartupError("aria2 RPC session is not started")
        return self._client.call(method, [f"token:{self._secret}", *params])


def _with_adaptive_aria2_context(
    event: ProgressEvent,
    options: FileDownloadOptions,
    scheduler: AdaptiveScheduler | None,
) -> ProgressEvent:
    if scheduler is None:
        return event
    updates: dict[str, object] = {
        "queue_concurrency": scheduler.current_concurrency,
        "per_host_concurrency": scheduler.per_host_concurrency,
        "per_file_segments": max(options.connections, options.splits),
        "max_total_connections": scheduler.max_total_connections,
        "max_per_host_connections": (
            min(
                scheduler.max_total_connections,
                scheduler.host_cap(_host_from_url(options.url))
                * max(options.connections, options.splits),
            )
        ),
        "selected_backend": "aria2",
        "scheduler_decision": _decision_label(scheduler),
    }
    if scheduler.current_speed_limit:
        updates["speed_limit"] = scheduler.current_speed_limit
    return event.model_copy(update=updates)


def progress_event_from_aria2_rpc_status(
    status: Mapping[str, object],
    *,
    filename: str | None,
    url: str | None,
    kind: HubKind = HubKind.file,
) -> ProgressEvent:
    rpc_status = _status_text(status)
    downloaded = _int_from_status(status.get("completedLength"))
    total = _int_from_status(status.get("totalLength"))
    speed = _int_from_status(status.get("downloadSpeed"))
    connections = _int_from_status(status.get("connections"))
    if total is not None and downloaded is not None and downloaded > total:
        total = downloaded
    eta = ((total - downloaded) / speed) if total and downloaded is not None and speed else None
    files = status.get("files")
    followed_by = _string_list(status.get("followedBy"))
    files_total, files_done = _file_counts(files)
    event_status = _progress_status(rpc_status)
    phase = ProgressPhase.error if event_status == "error" else ProgressPhase.download
    if event_status == "done":
        phase = ProgressPhase.done
    message = _aria2_status_message(status, rpc_status)
    return ProgressEvent(
        engine=EngineKind.aria2,
        phase=phase,
        kind=kind,
        status=event_status,
        filename=_status_filename(files) or filename,
        url=url,
        backend_id=_optional_str(status.get("gid")),
        error_code=_optional_str(status.get("errorCode")),
        downloaded_bytes=downloaded,
        total_bytes=total,
        verified_bytes=_int_from_status(status.get("verifiedLength")),
        verification_pending=_bool_from_status(status.get("verifyIntegrityPending")),
        piece_length=_int_from_status(status.get("pieceLength")),
        piece_count=_int_from_status(status.get("numPieces")),
        bitfield=_optional_str(status.get("bitfield")),
        followed_by=followed_by,
        following=_optional_str(status.get("following")),
        belongs_to=_optional_str(status.get("belongsTo")),
        backend_files=_file_payload(files),
        speed_bytes_per_sec=float(speed) if speed is not None else None,
        eta_seconds=float(eta) if eta is not None else None,
        active_connections=connections,
        files_done=files_done,
        files_total=files_total,
        message=message,
    )


def _rpc_download_options(
    options: FileDownloadOptions,
    output: Path,
    *,
    metalink: bool = False,
) -> dict[str, object]:
    rpc_options: dict[str, object] = {
        "dir": str(output.parent),
        "continue": "true" if options.continue_download else "false",
        "allow-overwrite": "true" if options.overwrite else "false",
        "auto-file-renaming": "false",
        "max-connection-per-server": str(options.connections),
        "split": str(options.splits),
        "min-split-size": options.chunk_size,
    }
    if options.filename or not metalink:
        rpc_options["out"] = output.name
    if options.rate_limit:
        rpc_options["max-download-limit"] = options.rate_limit
    if options.user_agent:
        rpc_options["user-agent"] = options.user_agent
    headers = list(options.headers)
    if options.referer:
        rpc_options["referer"] = options.referer
    if options.cache is False:
        headers.append("Cache-Control: no-cache")
    if options.no_compression:
        rpc_options["http-accept-gzip"] = "false"
        headers.append("Accept-Encoding: identity")
    elif options.compression:
        headers.append(f"Accept-Encoding: {options.compression}")
    if headers:
        rpc_options["header"] = headers
    if options.method and options.method != "GET":
        rpc_options["method"] = options.method
    if options.body_data:
        rpc_options["body-data"] = options.body_data
    if options.body_file:
        rpc_options["body-file"] = str(options.body_file)
    if options.load_cookies:
        rpc_options["load-cookies"] = str(options.load_cookies)
    if options.proxy:
        rpc_options["all-proxy"] = options.proxy
    if options.http_user:
        rpc_options["http-user"] = options.http_user
    if options.http_password:
        rpc_options["http-passwd"] = options.http_password
    if options.check_certificate is not None:
        rpc_options["check-certificate"] = "true" if options.check_certificate else "false"
    if options.ca_certificate:
        rpc_options["ca-certificate"] = str(options.ca_certificate)
    if options.certificate:
        rpc_options["certificate"] = str(options.certificate)
    if options.private_key:
        rpc_options["private-key"] = str(options.private_key)
    if options.secure_protocol:
        rpc_options["secure-protocol"] = options.secure_protocol
    if options.save_session:
        rpc_options["force-save"] = "true"
    if options.metalink_preferred_protocol:
        rpc_options["metalink-preferred-protocol"] = options.metalink_preferred_protocol.value
    if options.metalink_language:
        rpc_options["metalink-language"] = options.metalink_language
    if options.metalink_os:
        rpc_options["metalink-os"] = options.metalink_os
    if options.metalink_location:
        rpc_options["metalink-location"] = options.metalink_location
    if options.metalink_base_uri:
        rpc_options["metalink-base-uri"] = options.metalink_base_uri
    if options.metalink_enable_unique_protocol is not None:
        rpc_options["metalink-enable-unique-protocol"] = (
            "true" if options.metalink_enable_unique_protocol else "false"
        )
    if options.uri_selector:
        rpc_options["uri-selector"] = options.uri_selector.value
    if options.lowest_speed_limit:
        rpc_options["lowest-speed-limit"] = options.lowest_speed_limit
    if options.max_tries is not None:
        rpc_options["max-tries"] = str(options.max_tries)
    if options.retry_wait is not None:
        rpc_options["retry-wait"] = f"{options.retry_wait:g}"
    rpc_options["timeout"] = f"{options.timeout:g}"
    if options.connect_timeout is not None:
        rpc_options["connect-timeout"] = f"{options.connect_timeout:g}"
    if options.file_allocation:
        rpc_options["file-allocation"] = options.file_allocation
    if options.check_integrity:
        rpc_options["check-integrity"] = "true"
    if options.remote_time:
        rpc_options["remote-time"] = "true"
    if options.conditional_get:
        rpc_options["conditional-get"] = "true"
    if not options.http_accept_gzip and not options.no_compression:
        rpc_options["http-accept-gzip"] = "false"
    checksum = _parse_checksum(options.checksum)
    if checksum:
        algorithm, digest = checksum
        rpc_options["checksum"] = f"{_aria2_checksum_algorithm(algorithm)}={digest}"
        rpc_options["check-integrity"] = "true"
    return rpc_options


def _is_metalink_download(options: FileDownloadOptions) -> bool:
    return options.force_metalink or (options.metalink and is_metalink_url(options.url))


def _aria2_progress_kind(options: FileDownloadOptions) -> HubKind:
    return HubKind.manifest if _is_metalink_download(options) else HubKind.file


def _queued_success_result(
    item: Aria2RpcQueuedDownload,
    gids: Sequence[str],
    status: Mapping[str, object],
) -> Aria2RpcQueuedDownloadResult:
    completed_output = _completed_output_path(status, fallback=item.output)
    if not completed_output.exists():
        message = f"aria2c completed but output file was not found: {completed_output}"
        _emit_queue_error(item, message, status=status)
        return Aria2RpcQueuedDownloadResult(item=item, error=message)

    downloaded = completed_output.stat().st_size
    done_status = dict(status)
    done_status.setdefault("completedLength", str(downloaded))
    done_status.setdefault("totalLength", str(downloaded))
    _emit(
        item.progress_callback,
        ProgressEvent(
            engine=EngineKind.aria2,
            phase=ProgressPhase.done,
            kind=_aria2_progress_kind(item.options),
            status="done",
            filename=completed_output.name,
            url=item.options.url,
            backend_id=gids[0] if gids else None,
            downloaded_bytes=downloaded,
            total_bytes=_int_from_status(done_status.get("totalLength")) or downloaded,
            active_connections=0,
            message="aria2c RPC finished",
        ),
    )
    return Aria2RpcQueuedDownloadResult(
        item=item,
        result=Aria2RpcDownloadResult(
            gid=gids[0] if gids else "",
            output=completed_output,
            status=done_status,
        ),
    )


def _emit_queue_error(
    item: Aria2RpcQueuedDownload,
    message: str,
    *,
    status: Mapping[str, object] | None = None,
) -> None:
    _emit(
        item.progress_callback,
        ProgressEvent(
            engine=EngineKind.aria2,
            phase=ProgressPhase.error,
            kind=_aria2_progress_kind(item.options),
            status="error",
            filename=item.output.name,
            url=item.options.url,
            backend_id=_optional_str(status.get("gid")) if status else None,
            error_code=_optional_str(status.get("errorCode")) if status else None,
            message=message,
        ),
    )


def _fetch_metalink(options: FileDownloadOptions) -> bytes:
    headers = {"User-Agent": options.user_agent or "atlas/0.1"}
    if options.referer:
        headers["Referer"] = options.referer
    for header in options.headers:
        name, _separator, value = header.partition(":")
        headers[name.strip()] = value.strip()
    request = redirect_safe_request(options.url, headers=headers, method="GET")
    response_cm = (
        open_request(request, timeout=options.timeout, proxy=options.proxy)
        if options.proxy
        else urlopen(request, timeout=options.timeout)
    )
    with response_cm as response:
        declared_length = response.headers.get("Content-Length")
        if (
            declared_length
            and declared_length.isdigit()
            and int(declared_length) > _MAX_METALINK_BYTES
        ):
            raise EngineError(
                f"Metalink manifest exceeds the {_MAX_METALINK_BYTES}-byte safety limit"
            )
        data = response.read(_MAX_METALINK_BYTES + 1)
    if not isinstance(data, bytes):
        raise EngineError("Metalink response was not bytes")
    if len(data) > _MAX_METALINK_BYTES:
        raise EngineError(f"Metalink manifest exceeds the {_MAX_METALINK_BYTES}-byte safety limit")
    return data


def _aggregate_statuses(statuses: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    if len(statuses) == 1:
        return statuses[0]
    if any(_status_text(status) in {"error", "removed"} for status in statuses):
        for status in statuses:
            if _status_text(status) in {"error", "removed"}:
                return status
    complete = all(_status_text(status) == "complete" for status in statuses)
    active = any(_status_text(status) == "active" for status in statuses)
    status_text = "complete" if complete else "active" if active else "waiting"
    files: list[object] = []
    followed_by: list[str] = []
    for status in statuses:
        status_files = status.get("files")
        if isinstance(status_files, list):
            files.extend(status_files)
        followed_by.extend(_string_list(status.get("followedBy")))
    completed_length = sum(
        _int_from_status(status.get("completedLength")) or 0 for status in statuses
    )
    total_length = sum(_int_from_status(status.get("totalLength")) or 0 for status in statuses)
    download_speed = sum(_int_from_status(status.get("downloadSpeed")) or 0 for status in statuses)
    connections = sum(_int_from_status(status.get("connections")) or 0 for status in statuses)
    verified_length = sum(
        _int_from_status(status.get("verifiedLength")) or 0 for status in statuses
    )
    verification_pending = any(
        _bool_from_status(status.get("verifyIntegrityPending")) for status in statuses
    )
    gids = [gid for gid in (_status_gid(status) for status in statuses) if gid]
    return {
        "gid": ",".join(gids),
        "status": status_text,
        "completedLength": str(completed_length),
        "totalLength": str(total_length),
        "downloadSpeed": str(download_speed),
        "connections": str(connections),
        "verifiedLength": str(verified_length),
        "verifyIntegrityPending": verification_pending,
        "followedBy": followed_by,
        "files": files,
    }


def _completed_output_path(status: Mapping[str, object], *, fallback: Path) -> Path:
    files = status.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str) and path:
                candidate = Path(path)
                if candidate.exists():
                    return candidate
    return fallback


def _status_text(status: Mapping[str, object]) -> str:
    value = status.get("status")
    return value if isinstance(value, str) else "unknown"


def _progress_status(rpc_status: str) -> str:
    return {
        "active": "downloading",
        "waiting": "queued",
        "complete": "done",
        "error": "error",
        "removed": "error",
    }.get(rpc_status, "running")


def _aria2_status_message(status: Mapping[str, object], rpc_status: str) -> str | None:
    if rpc_status == "error":
        return _aria2_failure_message(status, rpc_status)
    if rpc_status == "removed":
        return "aria2 download was removed"
    return None


def _aria2_failure_message(status: Mapping[str, object], rpc_status: str) -> str:
    code = status.get("errorCode")
    message = status.get("errorMessage")
    if message:
        return f"aria2 {rpc_status} {code}: {message}"
    if code:
        return f"aria2 {rpc_status} {code}"
    return f"aria2 download {rpc_status}"


def _record_adaptive_aria2_status(
    scheduler: AdaptiveScheduler,
    status: Mapping[str, object],
    rpc_status: str,
    *,
    host: str | None = None,
) -> None:
    if rpc_status == "complete":
        scheduler.record_success(host=host)
        return
    if rpc_status not in {"error", "removed"}:
        return
    message = _aria2_failure_message(status, rpc_status)
    scheduler.record_backoff(
        status_code=_backoff_status_code(message),
        reason=_backoff_reason(message),
        host=host,
    )


def _decision_label(scheduler: AdaptiveScheduler) -> str:
    decision = scheduler.last_decision
    if decision.action == "stable":
        return "shared aria2 queue: adaptive max-concurrent-downloads"
    if decision.previous_cap == decision.new_cap:
        return f"{decision.action}: {decision.reason}"
    return (
        f"{decision.action}: {decision.scope} "
        f"{decision.previous_cap} -> {decision.new_cap}; {decision.reason}"
    )


def _host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname


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


def _int_from_status(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _status_gid(status: Mapping[str, object]) -> str | None:
    return _optional_str(status.get("gid"))


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _bool_from_status(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _file_payload(files: object) -> list[dict[str, object]]:
    if not isinstance(files, list):
        return []
    payload: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        safe: dict[str, object] = {}
        for key in ("index", "path", "length", "completedLength", "selected"):
            value = item.get(key)
            if isinstance(value, str | int | bool):
                safe[key] = value
        uris = item.get("uris")
        if isinstance(uris, list):
            safe["uris"] = [
                uri
                for uri in uris
                if isinstance(uri, dict)
                and all(isinstance(uri.get(key), str) for key in ("uri", "status") if key in uri)
            ]
        if safe:
            payload.append(safe)
    return payload


def _file_counts(files: object) -> tuple[int | None, int | None]:
    if not isinstance(files, list):
        return None, None
    done = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        length = _int_from_status(item.get("length"))
        completed = _int_from_status(item.get("completedLength"))
        if length is not None and completed is not None and completed >= length:
            done += 1
    return len(files), done


def _status_filename(files: object) -> str | None:
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            return str(Path(path).name)
    return None


def _parse_checksum(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    algorithm, digest = value.split(":", 1)
    return algorithm, digest


def _aria2_checksum_algorithm(algorithm: str) -> str:
    if algorithm == "sha256":
        return "sha-256"
    if algorithm == "sha512":
        return "sha-512"
    return algorithm


def _emit(callback: ProgressCallback | None, event: ProgressEvent | None) -> None:
    if callback and event:
        callback(event)
