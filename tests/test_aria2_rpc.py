from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from atlas.adaptive import AdaptiveScheduler
from atlas.aria2_rpc import (
    Aria2RpcQueuedDownload,
    Aria2RpcSession,
    progress_event_from_aria2_rpc_status,
)
from atlas.errors import EngineError
from atlas.models import (
    AdaptivePoliteness,
    Aria2UriSelector,
    EngineKind,
    FileBackendChoice,
    FileDownloadOptions,
    HubKind,
    MetalinkPreferredProtocol,
    ProgressEvent,
    ProgressPhase,
)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class StubbornProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls < 3:
            raise subprocess.TimeoutExpired("aria2c", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeRpcClient:
    def __init__(
        self,
        _endpoint: str,
        _timeout: float,
        *,
        statuses: list[dict[str, object]],
        output: Path | None = None,
        interrupt: bool = False,
    ) -> None:
        self.calls: list[tuple[str, list[object]]] = []
        self.statuses = statuses
        self.output = output
        self.interrupt = interrupt

    def call(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))
        if method == "aria2.getVersion":
            return {"version": "1.37.0"}
        if method == "aria2.addUri":
            return "gid-1"
        if method == "aria2.addMetalink":
            return ["gid-1"]
        if method == "aria2.tellStatus":
            if self.interrupt:
                raise KeyboardInterrupt
            status = self.statuses.pop(0)
            if status.get("status") == "complete" and self.output is not None:
                self.output.write_bytes(b"x" * 1024)
            return status
        if method in {"aria2.shutdown", "aria2.remove", "aria2.saveSession"}:
            return "OK"
        raise AssertionError(f"unexpected method {method}")


def test_progress_event_from_rpc_status_maps_structured_fields() -> None:
    event = progress_event_from_aria2_rpc_status(
        {
            "gid": "gid-1",
            "status": "active",
            "completedLength": "512",
            "totalLength": "1024",
            "downloadSpeed": "256",
            "connections": "3",
            "verifiedLength": "256",
            "verifyIntegrityPending": "true",
            "pieceLength": "128",
            "numPieces": "8",
            "bitfield": "f0",
            "followedBy": ["gid-2"],
            "following": "gid-parent",
            "belongsTo": "gid-group",
            "files": [
                {
                    "index": "1",
                    "path": "/tmp/archive.zip",
                    "length": "1024",
                    "completedLength": "512",
                }
            ],
        },
        filename="fallback.zip",
        url="https://example.com/archive.zip",
    )

    assert event == ProgressEvent(
        engine=EngineKind.aria2,
        phase=ProgressPhase.download,
        kind=HubKind.file,
        status="downloading",
        filename="archive.zip",
        url="https://example.com/archive.zip",
        backend_id="gid-1",
        downloaded_bytes=512,
        total_bytes=1024,
        verified_bytes=256,
        verification_pending=True,
        piece_length=128,
        piece_count=8,
        bitfield="f0",
        followed_by=["gid-2"],
        following="gid-parent",
        belongs_to="gid-group",
        speed_bytes_per_sec=256.0,
        eta_seconds=2.0,
        active_connections=3,
        files_done=0,
        files_total=1,
        backend_files=[
            {
                "index": "1",
                "path": "/tmp/archive.zip",
                "length": "1024",
                "completedLength": "512",
            }
        ],
    )


def test_progress_event_from_rpc_status_handles_waiting_and_removed() -> None:
    waiting = progress_event_from_aria2_rpc_status(
        {"status": "waiting", "completedLength": "0", "totalLength": "100"},
        filename="queued.bin",
        url="https://example.com/queued.bin",
    )
    removed = progress_event_from_aria2_rpc_status(
        {
            "status": "removed",
            "completedLength": "0",
            "totalLength": "100",
        },
        filename="removed.bin",
        url="https://example.com/removed.bin",
    )

    assert waiting.status == "queued"
    assert waiting.phase == ProgressPhase.download
    assert removed.status == "error"
    assert removed.phase == ProgressPhase.error
    assert removed.message == "aria2 download was removed"


def test_rpc_session_adds_uri_options_and_emits_progress(tmp_path: Path) -> None:
    output = tmp_path / "archive.zip"
    fake_process = FakeProcess()
    clients: list[FakeRpcClient] = []

    def popen_factory(args: list[str], **_kwargs: Any) -> FakeProcess:
        assert "--rpc-listen-all=false" in args
        assert "--rpc-secret=test-secret" in args
        assert "--rpc-listen-port=39001" in args
        assert f"--input-file={tmp_path / 'aria2.session'}" in args
        assert f"--save-session={tmp_path / 'aria2.next'}" in args
        assert "--force-save=true" in args
        assert "--save-session-interval=30" in args
        assert f"--server-stat-if={tmp_path / 'servers.in'}" in args
        assert f"--server-stat-of={tmp_path / 'servers.out'}" in args
        assert "--server-stat-timeout=3600" in args
        assert "--uri-selector=adaptive" in args
        return fake_process

    def client_factory(endpoint: str, timeout: float) -> FakeRpcClient:
        assert endpoint == "http://127.0.0.1:39001/jsonrpc"
        client = FakeRpcClient(
            endpoint,
            timeout,
            output=output,
            statuses=[
                {
                    "status": "active",
                    "completedLength": "400",
                    "totalLength": "1024",
                    "downloadSpeed": "200",
                    "connections": "2",
                    "files": [{"path": str(output), "length": "1024", "completedLength": "400"}],
                },
                {
                    "status": "complete",
                    "completedLength": "1024",
                    "totalLength": "1024",
                    "downloadSpeed": "0",
                    "connections": "0",
                    "files": [{"path": str(output), "length": "1024", "completedLength": "1024"}],
                },
            ],
        )
        clients.append(client)
        return client

    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        connections=8,
        splits=4,
        chunk_size="2M",
        checksum="sha256:" + "a" * 64,
        user_agent="AtlasTest/1.0",
        headers=("X-Test: yes",),
        referer="https://referrer.example/",
        cache=False,
        no_compression=True,
        method="POST",
        body_data="payload",
        lowest_speed_limit="32K",
        max_tries=5,
        retry_wait=2.5,
        connect_timeout=9,
        file_allocation="trunc",
        remote_time=True,
        conditional_get=True,
        input_file=tmp_path / "aria2.session",
        save_session=tmp_path / "aria2.next",
        save_session_interval=30,
        metalink_preferred_protocol=MetalinkPreferredProtocol.https,
        metalink_language="en-US",
        metalink_os="macos",
        metalink_location="us",
        metalink_base_uri="https://mirrors.example/releases/",
        metalink_enable_unique_protocol=False,
        server_stat_if=tmp_path / "servers.in",
        server_stat_of=tmp_path / "servers.out",
        server_stat_timeout=3600,
        uri_selector=Aria2UriSelector.adaptive,
    )
    events: list[ProgressEvent] = []

    result = Aria2RpcSession(
        executable="/opt/bin/aria2c",
        port_factory=lambda: 39001,
        token_factory=lambda: "test-secret",
        popen_factory=popen_factory,
        rpc_client_factory=client_factory,
        sleep=lambda _seconds: None,
    ).download(options, output, progress_callback=events.append)

    assert result.gid == "gid-1"
    assert result.output == output
    assert fake_process.wait_calls >= 1
    assert [event.status for event in events] == ["starting", "downloading", "done", "done"]
    assert events[1].active_connections == 2
    add_uri = next(call for call in clients[0].calls if call[0] == "aria2.addUri")
    assert add_uri[1][0] == "token:test-secret"
    assert add_uri[1][1] == ["https://example.com/archive.zip"]
    rpc_options = add_uri[1][2]
    assert isinstance(rpc_options, dict)
    assert rpc_options["dir"] == str(tmp_path)
    assert rpc_options["out"] == "archive.zip"
    assert rpc_options["continue"] == "true"
    assert rpc_options["allow-overwrite"] == "false"
    assert rpc_options["max-connection-per-server"] == "8"
    assert rpc_options["split"] == "4"
    assert rpc_options["min-split-size"] == "2M"
    assert rpc_options["user-agent"] == "AtlasTest/1.0"
    assert rpc_options["referer"] == "https://referrer.example/"
    assert rpc_options["header"] == [
        "X-Test: yes",
        "Cache-Control: no-cache",
        "Accept-Encoding: identity",
    ]
    assert rpc_options["http-accept-gzip"] == "false"
    assert rpc_options["method"] == "POST"
    assert rpc_options["body-data"] == "payload"
    assert rpc_options["lowest-speed-limit"] == "32K"
    assert rpc_options["max-tries"] == "5"
    assert rpc_options["retry-wait"] == "2.5"
    assert rpc_options["timeout"] == "30"
    assert rpc_options["connect-timeout"] == "9"
    assert rpc_options["file-allocation"] == "trunc"
    assert rpc_options["remote-time"] == "true"
    assert rpc_options["conditional-get"] == "true"
    assert rpc_options["force-save"] == "true"
    assert rpc_options["metalink-preferred-protocol"] == "https"
    assert rpc_options["metalink-language"] == "en-US"
    assert rpc_options["metalink-os"] == "macos"
    assert rpc_options["metalink-location"] == "us"
    assert rpc_options["metalink-base-uri"] == "https://mirrors.example/releases/"
    assert rpc_options["metalink-enable-unique-protocol"] == "false"
    assert rpc_options["uri-selector"] == "adaptive"
    assert rpc_options["checksum"] == "sha-256=" + "a" * 64
    assert rpc_options["check-integrity"] == "true"
    assert ("aria2.saveSession", ["token:test-secret"]) in clients[0].calls


def test_rpc_session_adds_metalink_manifest_and_uses_payload_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_output = tmp_path / "release.meta4"
    payload_output = tmp_path / "payload.bin"
    fake_process = FakeProcess()
    clients: list[FakeRpcClient] = []

    class FakeManifestResponse:
        def __enter__(self) -> FakeManifestResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<metalink />"

    def client_factory(endpoint: str, timeout: float) -> FakeRpcClient:
        client = FakeRpcClient(
            endpoint,
            timeout,
            output=payload_output,
            statuses=[
                {
                    "status": "complete",
                    "completedLength": "2048",
                    "totalLength": "2048",
                    "downloadSpeed": "0",
                    "connections": "0",
                    "files": [
                        {
                            "path": str(payload_output),
                            "length": "2048",
                            "completedLength": "2048",
                        }
                    ],
                },
            ],
        )
        clients.append(client)
        return client

    def fake_urlopen(request, *, timeout):
        assert request.full_url == "https://example.com/release.meta4"
        assert timeout == 30.0
        return FakeManifestResponse()

    monkeypatch.setattr("atlas.aria2_rpc.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/release.meta4",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )

    result = Aria2RpcSession(
        port_factory=lambda: 39004,
        token_factory=lambda: "test-secret",
        popen_factory=lambda _args, **_kwargs: fake_process,
        rpc_client_factory=client_factory,
        sleep=lambda _seconds: None,
    ).download(options, manifest_output)

    assert result.output == payload_output
    add_metalink = next(call for call in clients[0].calls if call[0] == "aria2.addMetalink")
    assert add_metalink[1][1] == "PG1ldGFsaW5rIC8+"
    rpc_options = add_metalink[1][2]
    assert isinstance(rpc_options, dict)
    assert rpc_options["dir"] == str(tmp_path)
    assert "out" not in rpc_options


def test_rpc_session_download_many_reuses_one_process_and_queue(
    tmp_path: Path,
) -> None:
    output_one = tmp_path / "one.bin"
    output_two = tmp_path / "two.bin"
    fake_process = FakeProcess()
    clients: list[BatchRpcClient] = []

    class BatchRpcClient:
        def __init__(self, _endpoint: str, _timeout: float) -> None:
            self.calls: list[tuple[str, list[object]]] = []
            self.added = 0
            self.statuses = {
                "gid-1": [
                    {
                        "gid": "gid-1",
                        "status": "active",
                        "completedLength": "10",
                        "totalLength": "20",
                        "downloadSpeed": "10",
                        "connections": "1",
                        "files": [
                            {
                                "path": str(output_one),
                                "length": "20",
                                "completedLength": "10",
                            }
                        ],
                    },
                    {
                        "gid": "gid-1",
                        "status": "complete",
                        "completedLength": "20",
                        "totalLength": "20",
                        "downloadSpeed": "0",
                        "connections": "0",
                        "files": [
                            {
                                "path": str(output_one),
                                "length": "20",
                                "completedLength": "20",
                            }
                        ],
                    },
                ],
                "gid-2": [
                    {
                        "gid": "gid-2",
                        "status": "active",
                        "completedLength": "5",
                        "totalLength": "10",
                        "downloadSpeed": "5",
                        "connections": "1",
                        "files": [
                            {
                                "path": str(output_two),
                                "length": "10",
                                "completedLength": "5",
                            }
                        ],
                    },
                    {
                        "gid": "gid-2",
                        "status": "complete",
                        "completedLength": "10",
                        "totalLength": "10",
                        "downloadSpeed": "0",
                        "connections": "0",
                        "files": [
                            {
                                "path": str(output_two),
                                "length": "10",
                                "completedLength": "10",
                            }
                        ],
                    },
                ],
            }

        def call(self, method: str, params: list[object]) -> object:
            self.calls.append((method, params))
            if method == "aria2.getVersion":
                return {"version": "1.37.0"}
            if method == "aria2.addUri":
                self.added += 1
                return f"gid-{self.added}"
            if method == "aria2.tellStatus":
                gid = str(params[1])
                status = self.statuses[gid].pop(0)
                if status["status"] == "complete":
                    Path(str(status["files"][0]["path"])).write_bytes(b"x" * 10)
                return status
            if method == "aria2.shutdown":
                return "OK"
            raise AssertionError(f"unexpected method {method}")

    def popen_factory(args: list[str], **_kwargs: Any) -> FakeProcess:
        assert "--max-concurrent-downloads=2" in args
        return fake_process

    def client_factory(endpoint: str, timeout: float) -> BatchRpcClient:
        client = BatchRpcClient(endpoint, timeout)
        clients.append(client)
        return client

    events_one: list[ProgressEvent] = []
    events_two: list[ProgressEvent] = []
    results = Aria2RpcSession(
        port_factory=lambda: 39005,
        token_factory=lambda: "test-secret",
        popen_factory=popen_factory,
        rpc_client_factory=client_factory,
        sleep=lambda _seconds: None,
        max_concurrent_downloads=2,
    ).download_many(
        [
            Aria2RpcQueuedDownload(
                options=FileDownloadOptions(
                    url="https://example.com/one.bin",
                    output_dir=tmp_path,
                    backend=FileBackendChoice.aria2,
                ),
                output=output_one,
                progress_callback=events_one.append,
            ),
            Aria2RpcQueuedDownload(
                options=FileDownloadOptions(
                    url="https://example.com/two.bin",
                    output_dir=tmp_path,
                    backend=FileBackendChoice.aria2,
                ),
                output=output_two,
                progress_callback=events_two.append,
            ),
        ]
    )

    assert [result.result.output for result in results if result.result] == [
        output_one,
        output_two,
    ]
    assert [event.status for event in events_one] == ["starting", "downloading", "done", "done"]
    assert [event.status for event in events_two] == ["starting", "downloading", "done", "done"]
    assert [call[0] for call in clients[0].calls].count("aria2.addUri") == 2
    assert fake_process.wait_calls >= 1


def test_rpc_session_download_many_applies_adaptive_backoff_options(
    tmp_path: Path,
) -> None:
    output_one = tmp_path / "one.bin"
    output_two = tmp_path / "two.bin"
    fake_process = FakeProcess()
    clients: list[AdaptiveRpcClient] = []

    class AdaptiveRpcClient:
        def __init__(self, _endpoint: str, _timeout: float) -> None:
            self.calls: list[tuple[str, list[object]]] = []
            self.added = 0
            self.statuses = {
                "gid-1": [
                    {
                        "gid": "gid-1",
                        "status": "error",
                        "completedLength": "0",
                        "totalLength": "20",
                        "downloadSpeed": "0",
                        "connections": "0",
                        "errorMessage": "disk saturation",
                        "files": [{"path": str(output_one), "length": "20"}],
                    }
                ],
                "gid-2": [
                    {
                        "gid": "gid-2",
                        "status": "complete",
                        "completedLength": "10",
                        "totalLength": "10",
                        "downloadSpeed": "0",
                        "connections": "0",
                        "files": [
                            {
                                "path": str(output_two),
                                "length": "10",
                                "completedLength": "10",
                            }
                        ],
                    }
                ],
            }

        def call(self, method: str, params: list[object]) -> object:
            self.calls.append((method, params))
            if method == "aria2.getVersion":
                return {"version": "1.37.0"}
            if method == "aria2.addUri":
                self.added += 1
                return f"gid-{self.added}"
            if method == "aria2.tellStatus":
                gid = str(params[1])
                status = self.statuses[gid].pop(0)
                if status["status"] == "complete":
                    Path(str(status["files"][0]["path"])).write_bytes(b"x" * 10)
                return status
            if method in {"aria2.changeGlobalOption", "aria2.shutdown"}:
                return "OK"
            raise AssertionError(f"unexpected method {method}")

    def client_factory(endpoint: str, timeout: float) -> AdaptiveRpcClient:
        client = AdaptiveRpcClient(endpoint, timeout)
        clients.append(client)
        return client

    scheduler = AdaptiveScheduler(
        max_concurrency=4,
        per_host_concurrency=4,
        politeness=AdaptivePoliteness.normal,
        min_concurrency=1,
    )
    scheduler.current_concurrency = 2

    results = Aria2RpcSession(
        port_factory=lambda: 39006,
        token_factory=lambda: "test-secret",
        popen_factory=lambda _args, **_kwargs: fake_process,
        rpc_client_factory=client_factory,
        sleep=lambda _seconds: None,
        max_concurrent_downloads=2,
    ).download_many(
        [
            Aria2RpcQueuedDownload(
                options=FileDownloadOptions(
                    url="https://example.com/one.bin",
                    output_dir=tmp_path,
                    backend=FileBackendChoice.aria2,
                ),
                output=output_one,
            ),
            Aria2RpcQueuedDownload(
                options=FileDownloadOptions(
                    url="https://example.com/two.bin",
                    output_dir=tmp_path,
                    backend=FileBackendChoice.aria2,
                ),
                output=output_two,
            ),
        ],
        adaptive_scheduler=scheduler,
    )

    assert results[0].error is not None
    assert "disk saturation" in results[0].error
    change_options = [
        call[1][1]
        for call in clients[0].calls
        if call[0] == "aria2.changeGlobalOption"
    ]
    assert {
        "max-concurrent-downloads": "1",
        "max-overall-download-limit": "1M",
    } in change_options


def test_rpc_session_error_status_raises_and_cleans_up(tmp_path: Path) -> None:
    output = tmp_path / "archive.zip"
    fake_process = FakeProcess()

    def client_factory(endpoint: str, timeout: float) -> FakeRpcClient:
        return FakeRpcClient(
            endpoint,
            timeout,
            statuses=[
                {
                    "status": "error",
                    "completedLength": "0",
                    "totalLength": "1024",
                    "downloadSpeed": "0",
                    "connections": "0",
                    "errorCode": "3",
                    "errorMessage": "resource not found",
                    "files": [{"path": str(output), "length": "1024", "completedLength": "0"}],
                }
            ],
        )

    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )
    events: list[ProgressEvent] = []

    with pytest.raises(EngineError, match="resource not found"):
        Aria2RpcSession(
            port_factory=lambda: 39002,
            token_factory=lambda: "test-secret",
            popen_factory=lambda _args, **_kwargs: fake_process,
            rpc_client_factory=client_factory,
            sleep=lambda _seconds: None,
        ).download(options, output, progress_callback=events.append)

    assert fake_process.wait_calls >= 1
    assert events[-1].status == "error"
    assert events[-1].message == "aria2 error 3: resource not found"


def test_rpc_session_cancellation_cleans_up_with_remove_and_kill(tmp_path: Path) -> None:
    output = tmp_path / "archive.zip"
    fake_process = StubbornProcess()
    clients: list[FakeRpcClient] = []

    def client_factory(endpoint: str, timeout: float) -> FakeRpcClient:
        client = FakeRpcClient(endpoint, timeout, statuses=[], interrupt=True)
        clients.append(client)
        return client

    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )

    with pytest.raises(KeyboardInterrupt):
        Aria2RpcSession(
            port_factory=lambda: 39003,
            token_factory=lambda: "test-secret",
            popen_factory=lambda _args, **_kwargs: fake_process,
            rpc_client_factory=client_factory,
            sleep=lambda _seconds: None,
        ).download(options, output)

    methods = [method for method, _params in clients[0].calls]
    assert "aria2.remove" in methods
    assert "aria2.shutdown" in methods
    assert fake_process.terminated is True
    assert fake_process.killed is True


def test_rpc_redacted_command_and_dry_run_do_not_expose_secret() -> None:
    command = Aria2RpcSession.redacted_command("/opt/bin/aria2c")

    assert command[0] == "/opt/bin/aria2c"
    assert "--rpc-secret=<redacted>" in command
    assert "test-secret" not in " ".join(command)
