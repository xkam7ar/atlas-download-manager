from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from urllib.error import URLError

import pytest

from atlas.aria2_rpc import Aria2RpcStartupError
from atlas.backends import (
    FileDownloadEngine,
    MirrorDownloadError,
    SiteMirrorEngine,
    _curl_file_args,
    _exact_directory_relative_url_path,
    _wget2_failed_urls,
    _write_http_metadata,
    filename_from_url,
    parse_wget2_stats_files,
    verify_checksum,
)
from atlas.doctor import Wget2Capabilities
from atlas.errors import EngineError
from atlas.models import (
    Aria2UriSelector,
    CertificateType,
    DirectFileProbe,
    DirectoryMirrorOptions,
    DownloadAttrMode,
    DownloadResult,
    DownloadStatus,
    EngineKind,
    FileBackendChoice,
    FileDownloadOptions,
    HttpsEnforceMode,
    HubKind,
    MetalinkPreferredProtocol,
    PreferFamily,
    ProgressEvent,
    ProgressPhase,
    SiteBackendChoice,
    SiteDownloadOptions,
    VerifySigMode,
    WorkItem,
)
from atlas.runner import ProcessCanceled, ProcessControl, SubprocessResult


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}
        self._url = url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int) -> bytes:
        body = self._body
        self._body = b""
        return body

    def geturl(self) -> str | None:
        return self._url


class _ChunkedFakeResponse(_FakeResponse):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(b"", status=status, headers=headers)
        self._chunks = chunks

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_filename_from_url_uses_path_name() -> None:
    assert filename_from_url("https://example.com/files/My%20File.zip?token=1") == "My File.zip"
    assert filename_from_url("https://example.com/") == "download"


def test_filename_from_url_sanitizes_hostile_and_long_names() -> None:
    hostile = "https://example.com/files/..%2F..%2Fbad%00name%0Awith%5Cslash%3F.txt"
    long_name = "https://example.com/files/" + ("a" * 260) + ".tar.gz"

    assert filename_from_url(hostile) == "bad_name_with_slash_.txt"
    sanitized_long = filename_from_url(long_name)
    assert sanitized_long.endswith(".gz")
    assert len(sanitized_long) <= 180


def test_file_engine_rejects_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    link = tmp_path / "archive.zip"
    link.symlink_to(target)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        dry_run=True,
    )

    with pytest.raises(EngineError, match="Refusing to write through symlink"):
        FileDownloadEngine().plan(options)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "relative/path",
        "data:text/plain,hello",
        "https://user:password@example.com/file.bin",
    ],
)
def test_http_download_models_reject_non_http_or_credentialed_urls(
    tmp_path: Path,
    url: str,
) -> None:
    with pytest.raises(ValueError, match="URL"):
        FileDownloadOptions(url=url, output_dir=tmp_path)
    with pytest.raises(ValueError, match="URL"):
        SiteDownloadOptions(url=url, output_dir=tmp_path)


def test_http_download_models_accept_ipv6_https_url(tmp_path: Path) -> None:
    url = "https://[2001:db8::1]:8443/files/archive.bin"

    assert FileDownloadOptions(url=url, output_dir=tmp_path).url == url
    assert SiteDownloadOptions(url=url, output_dir=tmp_path).url == url


def test_site_parser_input_mode_accepts_local_input_path(tmp_path: Path) -> None:
    source = tmp_path / "urls.txt"

    options = SiteDownloadOptions(
        url=str(source),
        output_dir=tmp_path,
        input_file=source,
        input_file_only=True,
    )

    assert options.url == str(source)


def test_site_parser_input_mode_still_validates_explicit_base(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        SiteDownloadOptions(
            url="relative/path",
            output_dir=tmp_path,
            input_file=tmp_path / "urls.txt",
            input_file_only=True,
            base="file:///etc/",
        )


def test_file_engine_sanitizes_explicit_filename(tmp_path: Path) -> None:
    options = FileDownloadOptions(
        url="https://example.com/download",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        filename="../evil\0name?.zip",
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.output == tmp_path / "evil_name_.zip"


def test_file_engine_builds_native_plan(tmp_path: Path) -> None:
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.backend == "native"
    assert plan.output == tmp_path / "archive.zip"
    assert plan.args == ["native", options.url, "--output", str(plan.output)]


def test_file_engine_builds_aria2_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        connections=8,
        splits=4,
        chunk_size="2M",
        checksum="sha256:" + "a" * 64,
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.backend == "aria2"
    assert plan.args[0] == "/opt/bin/aria2c"
    assert "--enable-rpc=true" in plan.args
    assert "--rpc-listen-all=false" in plan.args
    assert "--rpc-listen-port=<ephemeral>" in plan.args
    assert "--rpc-secret=<redacted>" in plan.args
    assert "--summary-interval=0" in plan.args
    assert "--show-console-readout=false" in plan.args
    assert "a" * 64 not in " ".join(plan.args)


def test_file_engine_builds_wget2_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.wget2,
        connections=6,
        chunk_size="4M",
        rate_limit="2M",
        max_tries=4,
        retry_wait=1.5,
        connect_timeout=7,
        user_agent="AtlasTest/1.0",
        headers=("X-Test: yes",),
        referer="https://referrer.example/",
        cache=False,
        compression="br",
        method="GET",
        load_cookies=tmp_path / "cookies.txt",
        proxy="http://127.0.0.1:8080",
        http_user="alice",
        http_password="secret",
        check_certificate=False,
        ca_certificate=tmp_path / "ca.pem",
        certificate=tmp_path / "client.pem",
        private_key=tmp_path / "client.key",
        secure_protocol="TLSv1_2",
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.backend == "wget2"
    assert plan.args[0] == "/opt/bin/wget2"
    assert "--output-document" in plan.args
    assert str(tmp_path / "archive.zip") in plan.args
    assert "--max-threads=6" in plan.args
    assert "--chunk-size=4M" in plan.args
    assert "--limit-rate=2M" in plan.args
    assert "--tries=4" in plan.args
    assert "--waitretry=1.5" in plan.args
    assert "--connect-timeout=7" in plan.args
    assert "--user-agent=AtlasTest/1.0" in plan.args
    assert "--header=X-Test: yes" in plan.args
    assert "--referer=https://referrer.example/" in plan.args
    assert "--no-cache" in plan.args
    assert "--compression=br" in plan.args
    assert f"--load-cookies={tmp_path / 'cookies.txt'}" in plan.args
    assert "--http-proxy=http://127.0.0.1:8080" in plan.args
    assert "--https-proxy=http://127.0.0.1:8080" in plan.args
    assert "--http-user=alice" in plan.args
    assert "--http-password=secret" in plan.args
    assert "--no-check-certificate" in plan.args
    assert f"--ca-certificate={tmp_path / 'ca.pem'}" in plan.args
    assert f"--certificate={tmp_path / 'client.pem'}" in plan.args
    assert f"--private-key={tmp_path / 'client.key'}" in plan.args
    assert "--secure-protocol=TLSv1_2" in plan.args
    assert plan.args[-1] == options.url


def test_file_engine_subprocess_aria2_args_include_policy_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        lowest_speed_limit="32K",
        max_tries=5,
        retry_wait=2.5,
        connect_timeout=9,
        file_allocation="trunc",
        check_integrity=True,
        remote_time=True,
        conditional_get=True,
        http_accept_gzip=False,
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

    args = FileDownloadEngine()._aria2_args(options, tmp_path / "archive.zip")

    assert "--no-conf=true" in args
    assert "--lowest-speed-limit=32K" in args
    assert "--max-tries=5" in args
    assert "--retry-wait=2.5" in args
    assert "--timeout=30" in args
    assert "--connect-timeout=9" in args
    assert "--file-allocation=trunc" in args
    assert "--check-integrity=true" in args
    assert "--remote-time=true" in args
    assert "--conditional-get=true" in args
    assert "--http-accept-gzip=false" in args
    assert f"--input-file={tmp_path / 'aria2.session'}" in args
    assert f"--save-session={tmp_path / 'aria2.next'}" in args
    assert "--force-save=true" in args
    assert "--save-session-interval=30" in args
    assert "--metalink-preferred-protocol=https" in args
    assert "--metalink-language=en-US" in args
    assert "--metalink-os=macos" in args
    assert "--metalink-location=us" in args
    assert "--metalink-base-uri=https://mirrors.example/releases/" in args
    assert "--metalink-enable-unique-protocol=false" in args
    assert f"--server-stat-if={tmp_path / 'servers.in'}" in args
    assert f"--server-stat-of={tmp_path / 'servers.out'}" in args
    assert "--server-stat-timeout=3600" in args
    assert "--uri-selector=adaptive" in args


def test_file_engine_aria2_rpc_startup_fallback_runs_one_shot_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "archive.zip"
    captured_args: list[str] = []

    class FailedRpcSession:
        @staticmethod
        def redacted_command(executable: str = "aria2c", **_kwargs: object) -> list[str]:
            return [executable, "--enable-rpc=true", "--rpc-secret=<redacted>"]

        def __init__(self, **_kwargs: object) -> None:
            return None

        def download(self, *_args: object, **_kwargs: object) -> object:
            raise Aria2RpcStartupError("RPC unavailable")

    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        captured_args.extend(args)
        output.write_bytes(b"downloaded")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.Aria2RpcSession", FailedRpcSession)
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert "--enable-rpc=true" not in captured_args
    assert "--rpc-secret=<redacted>" not in captured_args
    assert captured_args[-1] == options.url
    assert captured_args[captured_args.index("--out") + 1] == output.name


def test_file_engine_auto_routes_metalink_to_aria2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = FileDownloadOptions(
        url="https://example.com/release.meta4",
        output_dir=tmp_path,
        backend=FileBackendChoice.auto,
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.backend == "aria2"


def test_file_engine_can_save_metalink_manifest_when_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda _name: None)
    options = FileDownloadOptions(
        url="https://example.com/release.meta4",
        output_dir=tmp_path,
        backend=FileBackendChoice.auto,
        metalink=False,
    )

    plan = FileDownloadEngine().plan(options)

    assert plan.backend == "native"
    assert plan.output == tmp_path / "release.meta4"


def test_file_engine_aria2_emits_progress_events(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "archive.zip"

    def fake_run_args_stream(args, *, on_line, timeout):
        on_line("[#2089b0 400KiB/1MiB(39%) CN:1 DL:100KiB ETA:6s]")
        output.write_bytes(b"x" * 1024)
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )
    events: list[ProgressEvent] = []

    result = FileDownloadEngine().download(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert [event.status for event in events] == ["starting", "downloading", "done"]
    assert [event.engine for event in events] == [
        EngineKind.aria2,
        EngineKind.aria2,
        EngineKind.aria2,
    ]
    assert events[1].downloaded_bytes == 400 * 1024
    assert events[1].total_bytes == 1024 * 1024
    assert events[2].downloaded_bytes == 1024


def test_file_engine_aria2_uses_rpc_session(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "archive.zip"
    calls: list[tuple[FileDownloadOptions, Path]] = []

    class FakeSession:
        @staticmethod
        def redacted_command(executable: str = "aria2c", **_kwargs: object) -> list[str]:
            return [executable, "--rpc-secret=<redacted>"]

        def __init__(self, *, executable: str, **_kwargs: object) -> None:
            assert executable == "/opt/bin/aria2c"

        def download(self, options, output_path, *, progress_callback=None):
            output.write_bytes(b"x" * 10)
            calls.append((options, output_path))
            if progress_callback:
                progress_callback(
                    ProgressEvent(
                        engine=EngineKind.aria2,
                        status="done",
                        filename=output_path.name,
                    )
                )
            return type("Result", (), {"output": output_path})()

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.Aria2RpcSession", FakeSession)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )
    events: list[ProgressEvent] = []

    result = FileDownloadEngine().download(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert result.message == f"Saved to {output}"
    assert calls == [(options, output)]
    assert events[0].status == "done"


def test_file_engine_retries_tls_chain_failure_with_verified_curl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    captured_args: list[str] = []

    class FakeSession:
        @staticmethod
        def redacted_command(executable: str = "aria2c", **_kwargs: object) -> list[str]:
            return [executable, "--rpc-secret=<redacted>"]

        def __init__(self, **_kwargs: object) -> None:
            return None

        def download(self, *_args, **kwargs):
            progress_callback = kwargs.get("progress_callback")
            if progress_callback:
                progress_callback(
                    ProgressEvent(
                        engine=EngineKind.aria2,
                        status="error",
                        phase=ProgressPhase.error,
                        kind=HubKind.file,
                        message=(
                            "aria2 error 1: SSL/TLS handshake failure: "
                            "unable to get local issuer certificate"
                        ),
                    )
                )
            raise EngineError("SSL/TLS handshake failure: unable to get local issuer certificate")

    def fake_which(name: str) -> str | None:
        if name in {"aria2c", "curl"}:
            return f"/opt/bin/{name}"
        return None

    def fake_run_args_stream(args, *, on_line, timeout):
        captured_args.extend(args)
        output.write_bytes(b"downloaded")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", fake_which)
    monkeypatch.setattr("atlas.backends.Aria2RpcSession", FakeSession)
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
    )
    events: list[ProgressEvent] = []

    result = FileDownloadEngine().download(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert result.message == f"Saved to {output} (curl TLS fallback)"
    assert result.ydl_opts == {
        "backend": "curl",
        "fallback_from": "aria2",
        "output": str(output),
    }
    assert captured_args[:6] == [
        "/opt/bin/curl",
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--output",
    ]
    assert "--insecure" not in captured_args
    assert captured_args[-1] == options.url
    assert output.read_bytes() == b"downloaded"
    assert [(event.status, event.engine, event.phase) for event in events] == [
        ("retrying", EngineKind.curl, ProgressPhase.download),
        ("downloading", EngineKind.curl, ProgressPhase.download),
        ("running", EngineKind.curl, ProgressPhase.verify),
        ("done", EngineKind.curl, ProgressPhase.done),
    ]
    assert all(event.status not in {"error", "failed"} for event in events)


def test_verified_curl_overwrite_does_not_resume_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"stale")
    captured_args: list[str] = []

    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        captured_args.extend(args)
        output.write_bytes(b"fresh")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        continue_download=True,
        overwrite=True,
    )

    result = FileDownloadEngine().download_with_verified_curl(
        options,
        output=output,
        message="test fallback",
    )

    assert result.status == DownloadStatus.success
    assert "--continue-at" not in captured_args
    assert output.read_bytes() == b"fresh"


def test_verified_curl_restarts_unvalidated_partial_output(tmp_path: Path) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"partial")
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        continue_download=True,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=100,
            supports_ranges=True,
            etag='"current"',
        ),
    )

    args = _curl_file_args("/usr/bin/curl", options, output)

    assert "--continue-at" not in args
    assert not any("If-Range" in arg for arg in args)


def test_verified_curl_resumes_only_with_matching_strong_validator(tmp_path: Path) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"partial")
    _write_http_metadata(
        output,
        url="https://example.com/archive.zip",
        headers={"ETag": '"current"'},
    )
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        continue_download=True,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=100,
            supports_ranges=True,
            etag='"current"',
        ),
    )

    args = _curl_file_args("/usr/bin/curl", options, output)

    assert args[args.index("--continue-at") + 1] == "-"
    assert 'If-Range: "current"' in args


def test_file_engine_known_tls_probe_starts_directly_with_verified_curl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    captured_args: list[str] = []

    class UnexpectedSession:
        @staticmethod
        def redacted_command(executable: str = "aria2c", **_kwargs: object) -> list[str]:
            return [executable, "--rpc-secret=<redacted>"]

        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("known TLS probe should not start aria2")

    def fake_which(name: str) -> str | None:
        if name in {"aria2c", "curl"}:
            return f"/opt/bin/{name}"
        return None

    def fake_run_args_stream(args, *, on_line, timeout):
        captured_args.extend(args)
        output.write_bytes(b"downloaded")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", fake_which)
    monkeypatch.setattr("atlas.backends.Aria2RpcSession", UnexpectedSession)
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            error="TLS certificate verification failed",
        ),
    )
    events: list[ProgressEvent] = []

    result = FileDownloadEngine().download(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert captured_args[0] == "/opt/bin/curl"
    assert [(event.status, event.engine, event.phase) for event in events] == [
        ("downloading", EngineKind.curl, ProgressPhase.download),
        ("running", EngineKind.curl, ProgressPhase.verify),
        ("done", EngineKind.curl, ProgressPhase.done),
    ]


def test_file_engine_hard_constraints_disable_preemptive_curl_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_native(
        _self: FileDownloadEngine,
        options: FileDownloadOptions,
        _plan: object,
        *,
        progress_callback: object,
        max_output_bytes: int | None,
        required_url_scope: str | None,
        deadline: float | None,
    ) -> DownloadResult:
        _ = progress_callback, deadline
        seen.update(limit=max_output_bytes, scope=required_url_scope)
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="native constraints preserved",
        )

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(FileDownloadEngine, "_download_native", fake_native)
    monkeypatch.setattr(
        FileDownloadEngine,
        "_download_with_verified_curl_fallback",
        lambda *_args, **_kwargs: pytest.fail("curl shortcut bypassed hard constraints"),
    )
    options = FileDownloadOptions(
        url="https://example.com/root/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        probe=DirectFileProbe(
            url="https://example.com/root/archive.zip",
            error="TLS certificate verification failed",
        ),
    )

    result = FileDownloadEngine().download(
        options,
        max_output_bytes=1024,
        required_url_scope="https://example.com/root/",
    )

    assert result.status == DownloadStatus.success
    assert seen == {"limit": 1024, "scope": "https://example.com/root/"}


def test_file_engine_hard_constraints_disable_exception_curl_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_called = False

    def fail_native(*_args: object, **_kwargs: object) -> DownloadResult:
        raise EngineError("SSL/TLS handshake failure: unable to get local issuer certificate")

    def unexpected_fallback(*_args: object, **_kwargs: object) -> DownloadResult:
        nonlocal fallback_called
        fallback_called = True
        raise AssertionError("curl fallback bypassed hard constraints")

    monkeypatch.setattr(FileDownloadEngine, "_download_native", fail_native)
    monkeypatch.setattr(
        FileDownloadEngine,
        "_download_with_curl_after_tls_failure",
        unexpected_fallback,
    )
    options = FileDownloadOptions(
        url="https://example.com/root/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    with pytest.raises(EngineError, match="SSL/TLS handshake failure"):
        FileDownloadEngine().download(
            options,
            required_url_scope="https://example.com/root/",
        )

    assert fallback_called is False


def test_native_file_engine_retries_transient_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def flaky_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError("temporary connection failure")
        return _FakeResponse(b"ok", headers={"Content-Length": "2"})

    monkeypatch.setattr("atlas.backends.urlopen", flaky_urlopen)
    options = FileDownloadOptions(
        url="http://files.example/archive.bin",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        filename="archive.bin",
        max_tries=2,
        retry_wait=0,
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert attempts == 2
    assert (tmp_path / "archive.bin").read_bytes() == b"ok"


def test_native_file_engine_uses_explicit_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_open_request(request, *, timeout, context, proxy):
        seen.update(request=request, timeout=timeout, context=context, proxy=proxy)
        return _FakeResponse(b"ok", headers={"Content-Length": "2"})

    monkeypatch.setattr("atlas.backends.open_request", fake_open_request)
    monkeypatch.setattr(
        "atlas.backends.urlopen",
        lambda *_args, **_kwargs: pytest.fail("explicit proxy must not use bare urlopen"),
    )
    options = FileDownloadOptions(
        url="http://unresolvable.invalid/archive.bin",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        filename="archive.bin",
        proxy="http://proxy.example:8080",
        headers=("Authorization: Bearer secret", "X-Api-Key: secret"),
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert seen["proxy"] == "http://proxy.example:8080"
    request = seen["request"]
    assert request.full_url == options.url
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Bearer secret"
    assert request.unredirected_hdrs["X-api-key"] == "secret"
    assert (tmp_path / "archive.bin").read_bytes() == b"ok"


def test_native_file_engine_deadline_stops_slow_chunk_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}

    class SlowResponse(_FakeResponse):
        def read(self, _size: int) -> bytes:
            clock["now"] = 2.0
            return b"late"

    monkeypatch.setattr("atlas.backends.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "atlas.backends.urlopen",
        lambda *_args, **_kwargs: SlowResponse(
            b"",
            headers={"Content-Length": "4"},
        ),
    )
    options = FileDownloadOptions(
        url="http://files.example/archive.bin",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        filename="archive.bin",
    )

    with pytest.raises(EngineError, match="mirror max runtime"):
        FileDownloadEngine().download(options, deadline=1.0)

    assert not (tmp_path / "archive.bin").exists()


def test_native_rate_limit_cannot_sleep_past_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.time.monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "atlas.backends.time.sleep",
        lambda _seconds: pytest.fail("rate limiter slept past hard deadline"),
    )
    monkeypatch.setattr(
        "atlas.backends.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(
            b"x",
            headers={"Content-Length": "1"},
        ),
    )
    options = FileDownloadOptions(
        url="http://files.example/archive.bin",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        filename="archive.bin",
        rate_limit="1",
    )

    with pytest.raises(EngineError, match="mirror max runtime"):
        FileDownloadEngine().download(options, deadline=0.5)


def test_file_engine_wget2_emits_progress_events(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "archive.zip"

    def fake_run_args_stream(args, *, on_line, timeout):
        on_line("archive.zip 42% [=======>            ]")
        output.write_bytes(b"x" * 1024)
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.wget2,
    )
    events: list[ProgressEvent] = []

    result = FileDownloadEngine().download(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert result.ydl_opts == {"backend": "wget2", "output": str(output)}
    assert [event.status for event in events] == [
        "starting",
        "downloading",
        "running",
        "done",
    ]
    assert [event.engine for event in events] == [
        EngineKind.wget2,
        EngineKind.wget2,
        EngineKind.wget2,
        EngineKind.wget2,
    ]
    assert events[1].message == "archive.zip 42% [=======> ]"
    assert events[3].downloaded_bytes == 1024


def test_file_engine_wget2_rejects_success_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.wget2,
    )

    with pytest.raises(EngineError, match="reported success but no regular output file"):
        FileDownloadEngine().download(options)


def test_native_resume_refuses_unverified_partial_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"partial")

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("unsafe resume should fail before network I/O")

    monkeypatch.setattr("atlas.backends.urlopen", fail_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    with pytest.raises(EngineError, match="byte-range support"):
        FileDownloadEngine().download(options)

    assert output.read_bytes() == b"partial"


def test_native_resume_appends_only_after_range_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"hello ")
    output.with_name("archive.zip.atlas-http.json").write_text(
        '{"last_modified": "Wed, 21 Oct 2015 07:28:00 GMT"}\n',
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        seen["range"] = request.headers.get("Range")
        seen["if_range"] = request.headers.get("If-range")
        seen["accept_encoding"] = request.headers.get("Accept-encoding")
        seen["timeout"] = timeout
        return _FakeResponse(
            b"world",
            status=206,
            headers={
                "Content-Length": "5",
                "Content-Range": "bytes 6-10/11",
                "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            },
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=11,
            supports_ranges=True,
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        ),
        timeout=12,
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert output.read_bytes() == b"hello world"
    assert seen == {
        "range": "bytes=6-",
        "if_range": "Wed, 21 Oct 2015 07:28:00 GMT",
        "accept_encoding": "identity",
        "timeout": 12,
    }


def test_native_equal_length_without_identity_is_downloaded_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"old")
    output.with_name("archive.zip.atlas-http.json").write_text(
        '{"etag": "\\"stored-only\\""}\n',
        encoding="utf-8",
    )
    seen_ranges: list[str | None] = []

    def fake_urlopen(request, *, timeout):
        _ = timeout
        seen_ranges.append(request.headers.get("Range"))
        return _FakeResponse(b"new", headers={"Content-Length": "3"})

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=3,
            supports_ranges=True,
        ),
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert output.read_bytes() == b"new"
    assert seen_ranges == [None]


def test_native_resume_restarts_when_content_range_start_is_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"hello ")
    output.with_name("archive.zip.atlas-http.json").write_text(
        '{"etag": "\\"v1\\""}\n',
        encoding="utf-8",
    )
    seen_ranges: list[str | None] = []

    def fake_urlopen(request, *, timeout):
        _ = timeout
        requested_range = request.headers.get("Range")
        seen_ranges.append(requested_range)
        if requested_range:
            return _FakeResponse(
                b"bad",
                status=206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 5-7/11",
                    "ETag": '"v1"',
                },
            )
        return _FakeResponse(
            b"hello world",
            headers={"Content-Length": "11", "ETag": '"v1"'},
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=11,
            supports_ranges=True,
            etag='"v1"',
        ),
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert output.read_bytes() == b"hello world"
    assert seen_ranges == ["bytes=6-", None]


def test_native_timestamping_skips_current_local_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"data")
    output.touch()

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("current file should not be downloaded")

    monkeypatch.setattr("atlas.backends.urlopen", fail_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        timestamping=True,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=4,
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        ),
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.skipped
    assert output.read_bytes() == b"data"


def test_native_timestamping_sends_conditional_headers_and_updates_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"
    output.write_bytes(b"old")
    metadata = output.with_name("archive.zip.atlas-http.json")
    metadata.write_text(
        ('{"etag": "\\"old\\"", "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT"}'),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        seen["if_none_match"] = request.headers.get("If-none-match")
        seen["if_modified_since"] = request.headers.get("If-modified-since")
        seen["range"] = request.headers.get("Range")
        seen["timeout"] = timeout
        return _FakeResponse(
            b"new",
            headers={
                "Content-Length": "3",
                "ETag": '"new"',
                "Last-Modified": "Thu, 22 Oct 2015 07:28:00 GMT",
                "Content-Type": "application/zip",
            },
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        timestamping=True,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=3,
            etag='"probe"',
            last_modified="Wed, 23 Oct 2030 07:28:00 GMT",
        ),
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert output.read_bytes() == b"new"
    assert seen == {
        "if_none_match": '"old"',
        "if_modified_since": "Wed, 21 Oct 2015 07:28:00 GMT",
        "range": None,
        "timeout": 30.0,
    }
    assert '"etag": "\\"new\\""' in metadata.read_text(encoding="utf-8")
    assert '"content_type": "application/zip"' in metadata.read_text(encoding="utf-8")
    assert metadata.stat().st_mode & 0o777 == 0o600


def test_native_download_records_final_url_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"

    def fake_urlopen(request, *, timeout):
        return _FakeResponse(
            b"new",
            headers={"Content-Length": "3"},
            url="https://cdn.example.com/archive.zip",
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert output.read_bytes() == b"new"
    metadata = output.with_name("archive.zip.atlas-http.json").read_text(encoding="utf-8")
    assert '"final_url": "https://cdn.example.com/archive.zip"' in metadata


def test_native_metadata_redacts_signed_source_and_final_urls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "archive.zip"

    def fake_urlopen(request, *, timeout):
        return _FakeResponse(
            b"new",
            headers={"Content-Length": "3"},
            url="https://cdn.example/archive.zip?X-Goog-Signature=FINALSECRET",
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip?X-Amz-Signature=SOURCESECRET",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    metadata_path = output.with_name("archive.zip.atlas-http.json")
    metadata = metadata_path.read_text(encoding="utf-8")
    assert "SOURCESECRET" not in metadata
    assert "FINALSECRET" not in metadata
    assert "X-Amz-Signature=<redacted>" in metadata
    assert "X-Goog-Signature=<redacted>" in metadata
    assert metadata_path.stat().st_mode & 0o777 == 0o600


def test_native_rate_limit_throttles_chunk_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = {"value": 0.0}
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now["value"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    def fake_urlopen(request, *, timeout):
        return _ChunkedFakeResponse(
            [b"x" * 1024, b"y" * 1024],
            headers={"Content-Length": "2048"},
        )

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    monkeypatch.setattr("atlas.backends.time.monotonic", fake_monotonic)
    monkeypatch.setattr("atlas.backends.time.sleep", fake_sleep)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        rate_limit="1K",
    )

    result = FileDownloadEngine().download(options)

    assert result.status == DownloadStatus.success
    assert (tmp_path / "archive.zip").read_bytes() == b"x" * 1024 + b"y" * 1024
    assert sleeps == [1.0, 1.0]


def test_native_download_rejects_size_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_urlopen(request, *, timeout):
        return _FakeResponse(b"short", headers={"Content-Length": "10"})

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
    )

    with pytest.raises(EngineError, match="Downloaded size mismatch"):
        FileDownloadEngine().download(options)


def test_native_checksum_mismatch_never_publishes_unverified_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.backends.urlopen",
        lambda request, *, timeout: _FakeResponse(
            b"attacker-controlled",
            headers={"Content-Length": "19"},
        ),
    )
    options = FileDownloadOptions(
        url="https://example.com/payload.bin",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        checksum="sha256:" + "0" * 64,
    )

    with pytest.raises(EngineError, match="Checksum mismatch"):
        FileDownloadEngine().download(options)

    assert not (tmp_path / "payload.bin").exists()
    assert not list(tmp_path.glob(".payload.bin.atlas-part-*"))


def test_file_engine_rejects_known_download_larger_than_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = type("DiskUsage", (), {"free": 1024})()
    monkeypatch.setattr("atlas.backends.shutil.disk_usage", lambda _path: usage)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.native,
        probe=DirectFileProbe(
            url="https://example.com/archive.zip",
            content_length=2048,
        ),
    )

    with pytest.raises(EngineError, match="Insufficient free space"):
        FileDownloadEngine().download(options)


def test_verify_checksum_accepts_matching_digest(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("hello\n", encoding="utf-8")

    verify_checksum(path, "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03")


def test_verify_checksum_rejects_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("hello\n", encoding="utf-8")

    with pytest.raises(EngineError, match="Checksum mismatch"):
        verify_checksum(path, "sha256:" + "0" * 64)


def test_site_engine_builds_wget2_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        depth=3,
        span_hosts=True,
    )

    plan = SiteMirrorEngine().plan(options)

    assert plan.backend == "wget2"
    assert plan.args[0] == "/opt/bin/wget2"
    assert "--recursive" in plan.args
    assert "--level=3" in plan.args
    assert "--page-requisites" in plan.args
    assert "--convert-links" in plan.args
    assert "--span-hosts" in plan.args
    assert "--robots" in plan.args
    assert "--follow-sitemaps" in plan.args
    assert "--no-parent" in plan.args
    assert "--max-threads=5" in plan.args
    assert "--tries=20" in plan.args
    assert "--waitretry=10" in plan.args
    assert "--continue" in plan.args
    assert "--clobber" not in plan.args
    assert "--mirror" not in plan.args
    assert "--no-if-modified-since" not in plan.args


def test_redirecting_backends_reject_sensitive_custom_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    file_options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend=FileBackendChoice.aria2,
        headers=("X-Auth: SECURITY_SENTINEL",),
    )
    site_options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        headers=("Authentication: SECURITY_SENTINEL",),
    )

    with pytest.raises(EngineError, match="Sensitive custom headers require the native"):
        FileDownloadEngine().plan(file_options)
    with pytest.raises(EngineError, match="unsafe with recursive Wget redirects"):
        SiteMirrorEngine().plan(site_options)


def test_redirecting_backends_reject_secret_header_values_and_unsafe_basic_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    with pytest.raises(EngineError, match="Sensitive custom headers require the native"):
        FileDownloadEngine().plan(
            FileDownloadOptions(
                url="https://example.com/archive.zip",
                output_dir=tmp_path,
                backend=FileBackendChoice.wget2,
                headers=("X-Callback: https://api.example/?token=SECURITY_SENTINEL",),
            )
        )
    with pytest.raises(EngineError, match="aria2 can forward generated credentials"):
        FileDownloadEngine().plan(
            FileDownloadOptions(
                url="https://example.com/archive.zip",
                output_dir=tmp_path,
                backend=FileBackendChoice.aria2,
                http_user="alice",
                http_password="secret",
            )
        )
    with pytest.raises(EngineError, match="Request bodies require the native backend"):
        FileDownloadEngine().plan(
            FileDownloadOptions(
                url="https://example.com/submit",
                output_dir=tmp_path,
                backend=FileBackendChoice.wget2,
                method="POST",
                body_data="private arbitrary payload",
            )
        )
    with pytest.raises(EngineError, match="unsafe with GNU Wget"):
        SiteMirrorEngine().plan(
            SiteDownloadOptions(
                url="https://example.com/docs/",
                output_dir=tmp_path,
                backend=SiteBackendChoice.wget,
                http_user="alice",
                http_password="secret",
            )
        )
    with pytest.raises(EngineError, match="does not accept request bodies"):
        SiteMirrorEngine().plan(
            SiteDownloadOptions(
                url="https://example.com/docs/",
                output_dir=tmp_path,
                backend=SiteBackendChoice.wget2,
                method="POST",
                post_data="private arbitrary payload",
            )
        )


def test_conventional_site_plan_rejects_unenforceable_max_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    with pytest.raises(EngineError, match="cannot be guaranteed"):
        SiteMirrorEngine().plan(
            SiteDownloadOptions(
                url="https://example.com/docs/",
                output_dir=tmp_path,
                backend=SiteBackendChoice.wget2,
                max_files=10,
            )
        )


def test_native_basic_auth_is_initial_hop_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request, **_kwargs):
        seen["request"] = request
        return _FakeResponse(b"ok", headers={"Content-Length": "2"})

    monkeypatch.setattr("atlas.backends.urlopen", fake_urlopen)
    result = FileDownloadEngine().download(
        FileDownloadOptions(
            url="https://example.com/archive.zip",
            output_dir=tmp_path,
            backend=FileBackendChoice.native,
            http_user="alice",
            http_password="secret",
        )
    )

    assert result.status == DownloadStatus.success
    request = seen["request"]
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Basic YWxpY2U6c2VjcmV0"


def test_site_engine_builds_wget2_overwrite_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        overwrite=True,
    )

    plan = SiteMirrorEngine().plan(options)

    assert "--continue" in plan.args
    assert "--clobber" in plan.args


def test_directory_engine_builds_bounded_file_tree_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = DirectoryMirrorOptions(
        url="https://example.com/files/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        depth=2,
        accept="zip,7z,pdf,mp4",
        reject="html,tmp",
    )

    plan = SiteMirrorEngine().plan(options)

    assert plan.backend == "wget2"
    assert plan.args[:11] == [
        "/opt/bin/wget2",
        "--recursive",
        "--no-parent",
        "--mirror",
        "--continue",
        "--timestamping",
        "--no-if-modified-since",
        f"--directory-prefix={tmp_path}",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "--level=2",
        "--no-verbose",
    ]
    assert "--recursive" in plan.args
    assert "--mirror" in plan.args
    assert "--level=2" in plan.args
    assert "--no-parent" in plan.args
    assert "--continue" in plan.args
    assert "--timestamping" in plan.args
    assert "--no-if-modified-since" in plan.args
    assert f"--directory-prefix={tmp_path}" in plan.args
    assert "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" in plan.args
    assert "--span-hosts" not in plan.args
    assert "--page-requisites" not in plan.args
    assert "--convert-links" not in plan.args
    assert "--accept=zip,7z,pdf,mp4" in plan.args
    assert "--reject=html,tmp" in plan.args
    assert plan.args.count("--mirror") == 1
    assert plan.args.index("--level=2") > plan.args.index("--mirror")
    assert plan.args[-1] == options.url
    assert any("open HTTP directory" in warning for warning in plan.warnings)


def test_directory_engine_downloads_exact_text_index_file_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[FileDownloadOptions] = []

    def fake_download(
        _self: FileDownloadEngine,
        options: FileDownloadOptions,
        **_kwargs: object,
    ) -> DownloadResult:
        seen.append(options)
        options.output_dir.mkdir(parents=True, exist_ok=True)
        (options.output_dir / str(options.filename)).write_text("ok", encoding="utf-8")
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="saved",
        )

    monkeypatch.setattr(FileDownloadEngine, "download", fake_download)
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/sub/readme.txt",
                kind=HubKind.file,
                filename="sub/readme.txt",
            ),
        ),
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.success
    assert result.ydl_opts is not None
    assert result.ydl_opts["backend"] == "native-exact-index"
    assert seen[0].backend == FileBackendChoice.native
    assert seen[0].output_dir == tmp_path / "mirror" / "sub"
    assert seen[0].filename == "readme.txt"


def test_directory_engine_cancels_during_exact_index_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ProcessControl()

    def fake_download(
        _self: FileDownloadEngine,
        options: FileDownloadOptions,
        *,
        progress_callback,
        **_kwargs: object,
    ) -> DownloadResult:
        control.cancel("operator stop")
        assert progress_callback is not None
        progress_callback(
            ProgressEvent(
                engine=EngineKind.native,
                status="downloading",
                phase=ProgressPhase.download,
                kind=HubKind.file,
                filename=str(options.filename),
                url=options.url,
                downloaded_bytes=1,
            )
        )
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="saved",
        )

    monkeypatch.setattr(FileDownloadEngine, "download", fake_download)
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/readme.txt",
                kind=HubKind.file,
                filename="readme.txt",
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="Mirror canceled: operator stop"):
        SiteMirrorEngine().mirror(options, progress_callback=lambda _event: None, control=control)


def test_directory_engine_rejects_exact_index_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "mirror"
    output.mkdir()
    (output / "sub").symlink_to(outside, target_is_directory=True)
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=output,
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/sub/readme.txt",
                kind=HubKind.file,
                filename="sub/readme.txt",
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="escapes through a symlink"):
        SiteMirrorEngine().mirror(options)

    assert not (outside / "readme.txt").exists()


def test_directory_engine_rejects_exact_index_file_directory_collision(
    tmp_path: Path,
) -> None:
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/bundle",
                kind=HubKind.file,
                filename="bundle",
            ),
            WorkItem(
                url="https://files.example/root/bundle/readme.txt",
                kind=HubKind.file,
                filename="bundle/readme.txt",
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="collide as files and folders"):
        SiteMirrorEngine().mirror(options)


def test_directory_engine_rejects_unicode_normalization_collision(tmp_path: Path) -> None:
    composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt"
    decomposed = "cafe\N{COMBINING ACUTE ACCENT}.txt"
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url=f"https://files.example/root/{composed}",
                kind=HubKind.file,
                filename=composed,
            ),
            WorkItem(
                url=f"https://files.example/root/{decomposed}",
                kind=HubKind.file,
                filename=decomposed,
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="collide as files and folders"):
        SiteMirrorEngine().mirror(options)


def test_directory_engine_recomputes_exact_index_scope_and_redacts_url(
    tmp_path: Path,
) -> None:
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url=("https://evil.example/root/archive.bin?X-Amz-Signature=do-not-leak"),
                kind=HubKind.file,
                filename="archive.bin",
                same_host=True,
                external_host=False,
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="escaped the requested origin") as raised:
        SiteMirrorEngine().mirror(options)

    assert "do-not-leak" not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_directory_engine_rejects_exact_index_redirect_outside_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.backends.open_request",
        lambda _request, **_kwargs: _FakeResponse(
            b"evil",
            headers={"Content-Length": "4"},
            url="https://evil.example/root/archive.bin",
        ),
    )
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/archive.bin",
                kind=HubKind.file,
                filename="archive.bin",
                content_length=4,
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="escaped the requested origin"):
        SiteMirrorEngine().mirror(options)

    assert not (tmp_path / "mirror" / "archive.bin").exists()


def test_directory_engine_rejects_double_encoded_redirect_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.backends.open_request",
        lambda _request, **_kwargs: _FakeResponse(
            b"evil",
            headers={"Content-Length": "4"},
            url="https://files.example/root/%252e%252e/secret.bin",
        ),
    )
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/archive.bin",
                kind=HubKind.file,
                filename="archive.bin",
                content_length=4,
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="Unsafe directory index URL refused"):
        SiteMirrorEngine().mirror(options)

    assert not (tmp_path / "mirror" / "archive.bin").exists()


def test_exact_directory_scope_preserves_valid_literal_percent_filename() -> None:
    relative = _exact_directory_relative_url_path(
        "https://files.example/root/",
        "https://files.example/root/100%25-free.txt",
    )

    assert relative.as_posix() == "100%-free.txt"


def test_directory_engine_enforces_exact_index_file_and_size_limits(tmp_path: Path) -> None:
    items = (
        WorkItem(
            url="https://files.example/root/one.bin",
            kind=HubKind.file,
            filename="one.bin",
            content_length=800,
        ),
        WorkItem(
            url="https://files.example/root/two.bin",
            kind=HubKind.file,
            filename="two.bin",
            content_length=800,
        ),
    )
    base = {
        "url": "https://files.example/root/",
        "output_dir": tmp_path / "mirror",
        "exact_directory_index": True,
        "exact_directory_items": items,
    }

    with pytest.raises(MirrorDownloadError, match="exceeding max-files 1"):
        SiteMirrorEngine().mirror(DirectoryMirrorOptions(**base, max_files=1))
    with pytest.raises(MirrorDownloadError, match=r"exceeding.*total-size limit 1K"):
        SiteMirrorEngine().mirror(DirectoryMirrorOptions(**base, max_total_size="1K"))


def test_directory_engine_hard_caps_stale_exact_index_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.backends.open_request",
        lambda _request, **_kwargs: _FakeResponse(
            b"x" * 2048,
            headers={"Content-Length": "512"},
        ),
    )
    output = tmp_path / "mirror" / "archive.bin"
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=output.parent,
        max_total_size="1K",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/archive.bin",
                kind=HubKind.file,
                filename="archive.bin",
                content_length=512,
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="1024-byte output limit"):
        SiteMirrorEngine().mirror(options)

    assert not output.exists()


def test_directory_engine_refuses_unbounded_exact_index_when_size_limit_set(
    tmp_path: Path,
) -> None:
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        max_total_size="1G",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/unknown.bin",
                kind=HubKind.file,
                filename="unknown.bin",
            ),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="limit cannot be guaranteed"):
        SiteMirrorEngine().mirror(options)


def test_directory_engine_checks_exact_index_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = type("DiskUsage", (), {"free": 1000})()
    monkeypatch.setattr("atlas.backends.shutil.disk_usage", lambda _path: usage)
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(
                url="https://files.example/root/one.bin",
                kind=HubKind.file,
                filename="one.bin",
                content_length=700,
            ),
            WorkItem(
                url="https://files.example/root/two.bin",
                kind=HubKind.file,
                filename="two.bin",
                content_length=700,
            ),
        ),
    )

    with pytest.raises(EngineError, match="Insufficient free space"):
        SiteMirrorEngine().mirror(options)


def test_directory_engine_refuses_non_http_exact_index_url(tmp_path: Path) -> None:
    options = DirectoryMirrorOptions(
        url="https://files.example/root/",
        output_dir=tmp_path / "mirror",
        exact_directory_index=True,
        exact_directory_items=(
            WorkItem(url="file:///etc/passwd", kind=HubKind.file, filename="passwd"),
        ),
    )

    with pytest.raises(MirrorDownloadError, match="Unsafe directory index URL"):
        SiteMirrorEngine().mirror(options)


def test_site_engine_builds_wget2_policy_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        robots=False,
        follow_sitemaps=False,
        domains="example.com,static.example.com",
        exclude_domains="ads.example.com",
        include_directories="/docs,/assets",
        exclude_directories="/private",
        accept_regex=".*\\.html$",
        reject_regex="logout",
        filter_mime_type="text/html",
        filter_urls=True,
        ignore_case=True,
        follow_tags="img/data-src,source/srcset",
        ignore_tags="a/href",
        directories=False,
        host_directories=False,
        protocol_directories=True,
        cut_dirs=2,
        default_page="home.html",
        adjust_extension=True,
        convert_file_only=True,
        cut_url_get_vars=True,
        cut_file_get_vars=True,
        keep_extension=True,
        unlink=True,
        backups=1,
        backup_converted=True,
        restrict_file_names="windows",
        download_attr=DownloadAttrMode.strip_path,
        input_file=tmp_path / "urls.txt",
        base="https://example.com/",
        force_html=True,
        force_css=True,
        force_sitemap=True,
        force_atom=True,
        force_rss=True,
        force_metalink=True,
        warc_file=tmp_path / "archive.warc.gz",
        warc_compression=True,
        warc_cdx=True,
        warc_max_size="1G",
        user_agent="AtlasTest/1.0",
        headers=("Accept-Language: en", "X-Test: yes"),
        referer="https://referrer.example/",
        cache=False,
        compression="br",
        method="GET",
        cookies=False,
        load_cookies=tmp_path / "cookies.txt",
        save_cookies=tmp_path / "saved-cookies.txt",
        keep_session_cookies=True,
        cookie_suffixes="public_suffixes.dat",
        netrc=False,
        netrc_file=tmp_path / "netrc",
        proxy=False,
        http_user="alice",
        http_password="secret",
        proxy_user="proxy-user",
        proxy_password="proxy-secret",
        https_only=True,
        https_enforce=HttpsEnforceMode.hard,
        hsts=False,
        hsts_file=tmp_path / "hsts.db",
        check_certificate=False,
        check_hostname=False,
        ca_certificate=tmp_path / "ca.pem",
        ca_directory=tmp_path / "ca-dir",
        certificate=tmp_path / "client.pem",
        certificate_type=CertificateType.pem,
        private_key=tmp_path / "client.key",
        private_key_type=CertificateType.der,
        crl_file=tmp_path / "revocations.pem",
        secure_protocol="TLSv1_2",
        ocsp=True,
        ocsp_date=False,
        ocsp_file=tmp_path / "ocsp.db",
        ocsp_nonce=False,
        ocsp_server="http://ocsp.example/",
        ocsp_stapling=True,
        tls_false_start=True,
        tls_resume=True,
        tls_session_file=tmp_path / "tls-sessions.db",
        http2=True,
        http2_only=True,
        http2_request_window=12,
        content_on_error=True,
        save_content_on="500,502",
        save_headers=True,
        server_response=True,
        ignore_length=True,
        verify_sig=VerifySigMode.no_fail,
        signature_extensions="asc,sig",
        gnupg_homedir=tmp_path / "gnupg",
        verify_save_failed=True,
        max_total_size="10M",
        max_runtime=45,
        quota="10M",
        limit_rate="1M",
        retry_connrefused=True,
        start_pos="1024",
        inet4_only=True,
        bind_address="127.0.0.1",
        bind_interface="lo0",
        prefer_family=PreferFamily.ipv4,
        dns_cache=False,
        dns_cache_preload=tmp_path / "dns-cache.txt",
        tcp_fastopen=False,
        max_threads=7,
        tries=3,
        waitretry=2.5,
        retry_on_http_error="429,503",
        max_redirect=4,
        timeout=9,
        dns_timeout=1,
        connect_timeout=2,
        read_timeout=3,
        random_wait=True,
        timestamping=True,
        spider=True,
    )

    plan = SiteMirrorEngine().plan(options)

    assert "--no-robots" in plan.args
    assert "--no-follow-sitemaps" in plan.args
    assert "--domains=example.com,static.example.com" in plan.args
    assert "--exclude-domains=ads.example.com" in plan.args
    assert "--include-directories=/docs,/assets" in plan.args
    assert "--exclude-directories=/private" in plan.args
    assert "--accept-regex=.*\\.html$" in plan.args
    assert "--reject-regex=logout" in plan.args
    assert "--filter-mime-type=text/html" in plan.args
    assert "--filter-urls" in plan.args
    assert "--ignore-case" in plan.args
    assert "--follow-tags=img/data-src,source/srcset" in plan.args
    assert "--ignore-tags=a/href" in plan.args
    assert "--no-directories" in plan.args
    assert "--no-host-directories" in plan.args
    assert "--protocol-directories" in plan.args
    assert "--cut-dirs=2" in plan.args
    assert "--default-page=home.html" in plan.args
    assert "--adjust-extension" in plan.args
    assert "--convert-file-only" in plan.args
    assert "--cut-url-get-vars" in plan.args
    assert "--cut-file-get-vars" in plan.args
    assert "--keep-extension" in plan.args
    assert "--unlink" in plan.args
    assert "--backups=1" in plan.args
    assert "--backup-converted" in plan.args
    assert "--restrict-file-names=windows" in plan.args
    assert "--download-attr=strippath" in plan.args
    assert f"--input-file={tmp_path / 'urls.txt'}" in plan.args
    assert "--base=https://example.com/" in plan.args
    assert "--force-html" in plan.args
    assert "--force-css" in plan.args
    assert "--force-sitemap" in plan.args
    assert "--force-atom" in plan.args
    assert "--force-rss" in plan.args
    assert "--force-metalink" in plan.args
    assert f"--warc-file={tmp_path / 'archive'}" in plan.args
    assert "--warc-compression" in plan.args
    assert "--warc-cdx" in plan.args
    assert "--warc-max-size=1G" in plan.args
    assert "--user-agent=AtlasTest/1.0" in plan.args
    assert "--header=Accept-Language: en" in plan.args
    assert "--header=X-Test: yes" in plan.args
    assert "--referer=https://referrer.example/" in plan.args
    assert "--no-cache" in plan.args
    assert "--compression=br" in plan.args
    assert "--no-cookies" in plan.args
    assert f"--load-cookies={tmp_path / 'cookies.txt'}" in plan.args
    assert f"--save-cookies={tmp_path / 'saved-cookies.txt'}" in plan.args
    assert "--keep-session-cookies" in plan.args
    assert "--cookie-suffixes=public_suffixes.dat" in plan.args
    assert "--no-netrc" in plan.args
    assert f"--netrc-file={tmp_path / 'netrc'}" in plan.args
    assert "--no-proxy" in plan.args
    assert "--http-user=alice" in plan.args
    assert "--http-password=secret" in plan.args
    assert "--proxy-user=proxy-user" in plan.args
    assert "--proxy-password=proxy-secret" in plan.args
    assert "--https-only" in plan.args
    assert "--https-enforce=hard" in plan.args
    assert "--no-hsts" in plan.args
    assert f"--hsts-file={tmp_path / 'hsts.db'}" in plan.args
    assert "--no-check-certificate" in plan.args
    assert "--no-check-hostname" in plan.args
    assert f"--ca-certificate={tmp_path / 'ca.pem'}" in plan.args
    assert f"--ca-directory={tmp_path / 'ca-dir'}" in plan.args
    assert f"--certificate={tmp_path / 'client.pem'}" in plan.args
    assert "--certificate-type=PEM" in plan.args
    assert f"--private-key={tmp_path / 'client.key'}" in plan.args
    assert "--private-key-type=DER" in plan.args
    assert f"--crl-file={tmp_path / 'revocations.pem'}" in plan.args
    assert "--secure-protocol=TLSv1_2" in plan.args
    assert "--ocsp" in plan.args
    assert "--no-ocsp-date" in plan.args
    assert f"--ocsp-file={tmp_path / 'ocsp.db'}" in plan.args
    assert "--no-ocsp-nonce" in plan.args
    assert "--ocsp-server=http://ocsp.example/" in plan.args
    assert "--ocsp-stapling" in plan.args
    assert "--tls-false-start" in plan.args
    assert "--tls-resume" in plan.args
    assert f"--tls-session-file={tmp_path / 'tls-sessions.db'}" in plan.args
    assert "--http2" in plan.args
    assert "--http2-only" in plan.args
    assert "--http2-request-window=12" in plan.args
    assert "--content-on-error" in plan.args
    assert "--save-content-on=500,502" in plan.args
    assert "--save-headers" in plan.args
    assert "--server-response" in plan.args
    assert "--ignore-length" in plan.args
    assert "--verify-sig=no-fail" in plan.args
    assert "--signature-extensions=asc,sig" in plan.args
    assert f"--gnupg-homedir={tmp_path / 'gnupg'}" in plan.args
    assert "--verify-save-failed" in plan.args
    assert "--quota=10M" in plan.args
    assert "--limit-rate=1M" in plan.args
    assert any("runtime is capped" in warning for warning in plan.warnings)
    assert "--retry-connrefused" in plan.args
    assert "--start-pos=1024" in plan.args
    assert "--inet4-only" in plan.args
    assert "--bind-address=127.0.0.1" in plan.args
    assert "--bind-interface=lo0" in plan.args
    assert "--prefer-family=IPv4" in plan.args
    assert "--no-dns-cache" in plan.args
    assert f"--dns-cache-preload={tmp_path / 'dns-cache.txt'}" in plan.args
    assert "--no-tcp-fastopen" in plan.args
    assert "--max-threads=7" in plan.args
    assert "--tries=3" in plan.args
    assert "--waitretry=2.5" in plan.args
    assert "--retry-on-http-error=429,503" in plan.args
    assert "--max-redirect=4" in plan.args
    assert "--timeout=9" in plan.args
    assert "--dns-timeout=1" in plan.args
    assert "--connect-timeout=2" in plan.args
    assert "--read-timeout=3" in plan.args
    assert "--random-wait" in plan.args
    assert "--timestamping" in plan.args
    assert "--spider" in plan.args


def test_site_engine_rejects_conflicting_total_size_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        max_total_size="10M",
        quota="20M",
    )

    with pytest.raises(EngineError, match="max-total-size"):
        SiteMirrorEngine().plan(options)


def test_site_mirror_stops_on_max_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["wget2"], 1.5)

    monkeypatch.setattr("atlas.backends.run_args_stream", timeout)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        max_runtime=1.5,
    )

    with pytest.raises(EngineError, match="max runtime"):
        SiteMirrorEngine().mirror(options)


def test_site_mirror_runtime_budget_includes_discovery_time(tmp_path: Path) -> None:
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        max_runtime=5,
        planning_runtime_seconds=5,
    )

    with pytest.raises(MirrorDownloadError, match="discovery used the available budget"):
        SiteMirrorEngine().mirror(options)


def test_site_mirror_timeout_preserves_wget2_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    def timeout(args, **_kwargs):
        for arg in args:
            if arg.startswith("--stats-site=csv:"):
                Path(arg.split(":", 1)[1]).write_text(
                    "host,files\nexample.com,2\n",
                    encoding="utf-8",
                )
        raise subprocess.TimeoutExpired(args, 1.5)

    monkeypatch.setattr("atlas.backends.run_args_stream", timeout)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path / "mirror",
        backend=SiteBackendChoice.wget2,
        max_runtime=1.5,
    )

    with pytest.raises(EngineError, match="max runtime") as raised:
        SiteMirrorEngine().mirror(options)

    assert raised.value.stats["summary"]["site"]["urls"] == 1


def test_site_mirror_reports_operator_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    def canceled(args, *, on_line, timeout, control):
        _ = on_line, timeout
        for arg in args:
            if arg.startswith("--stats-site=csv:"):
                Path(arg.split(":", 1)[1]).write_text(
                    "host,files\nexample.com,1\n",
                    encoding="utf-8",
                )
        assert isinstance(control, ProcessControl)
        control.cancel("operator stop")
        raise ProcessCanceled(args, control.reason)

    monkeypatch.setattr("atlas.backends.run_args_stream", canceled)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
    )
    events: list[ProgressEvent] = []

    with pytest.raises(EngineError, match="Mirror canceled: operator stop") as raised:
        SiteMirrorEngine().mirror(
            options,
            progress_callback=events.append,
            control=ProcessControl(),
        )

    assert [event.status for event in events] == ["starting", "canceled"]
    assert raised.value.stats["summary"]["site"]["urls"] == 1


def test_fresh_mirror_rejects_success_without_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.backends.run_args_stream",
        lambda args, **_kwargs: SubprocessResult(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    options = DirectoryMirrorOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path / "mirror",
        backend=SiteBackendChoice.wget2,
    )

    with pytest.raises(EngineError, match="no downloaded payload"):
        SiteMirrorEngine().mirror(options)


def test_conventional_mirror_refuses_existing_descendant_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "mirror"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "redirected-subtree").symlink_to(outside, target_is_directory=True)
    started = False

    def unexpected_run(*_args: object, **_kwargs: object) -> SubprocessResult:
        nonlocal started
        started = True
        raise AssertionError("mirror subprocess should not start")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", unexpected_run)
    options = DirectoryMirrorOptions(
        url="https://example.com/docs/",
        output_dir=output,
        backend=SiteBackendChoice.wget2,
    )

    with pytest.raises(MirrorDownloadError, match="containing a symbolic link"):
        SiteMirrorEngine().mirror(options)

    assert not started


def test_mirror_spider_does_not_require_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.backends.run_args_stream",
        lambda args, **_kwargs: SubprocessResult(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path / "mirror",
        backend=SiteBackendChoice.wget2,
        spider=True,
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.success
    assert result.ydl_opts is not None
    assert result.ydl_opts["payload_required"] is False


def test_idempotent_mirror_with_existing_payload_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "mirror"
    output.mkdir()
    (output / "index.html").write_text("stable", encoding="utf-8")
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.backends.run_args_stream",
        lambda args, **_kwargs: SubprocessResult(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    options = DirectoryMirrorOptions(
        url="https://example.com/docs/",
        output_dir=output,
        backend=SiteBackendChoice.wget2,
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.skipped
    assert result.ydl_opts is not None
    assert result.ydl_opts["no_change"] is True


def test_site_engine_input_file_only_plan_omits_positional_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    input_file = tmp_path / "urls.txt"
    input_file.write_text("https://example.com/sitemap.xml\n", encoding="utf-8")
    options = SiteDownloadOptions(
        url="https://example.com/",
        output_dir=tmp_path / "mirror",
        backend=SiteBackendChoice.wget2,
        input_file=input_file,
        input_file_only=True,
        force_sitemap=True,
        base="https://example.com/",
    )

    plan = SiteMirrorEngine().plan(options)

    assert f"--input-file={input_file}" in plan.args
    assert "--force-sitemap" in plan.args
    assert "--base=https://example.com/" in plan.args
    assert plan.args[-1] != "https://example.com/"


def test_site_engine_warns_for_missing_wget2_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.doctor._wget2_capabilities",
        lambda: Wget2Capabilities(
            path="/opt/bin/wget2",
            version="GNU Wget2 2.2.1",
            features={
                "http2": False,
                "brotli": False,
                "https": True,
                "ssl": True,
                "psl": False,
                "gpgme": False,
                "hsts": False,
                "idn2": False,
            },
        ),
    )
    options = SiteDownloadOptions(
        url="https://exämple.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        http2=True,
        compression="br",
        load_cookies=tmp_path / "cookies.txt",
        force_metalink=True,
        verify_sig=VerifySigMode.fail,
        hsts=True,
    )

    plan = SiteMirrorEngine().plan(options)

    assert plan.warnings == [
        "selected HTTP/2 options, but this wget2 build lacks +http2",
        "selected Brotli compression, but this wget2 build lacks +brotli",
        "selected HSTS persistence, but this wget2 build lacks +hsts",
        (
            "selected cookie store options, but this wget2 build lacks +psl; "
            "cookie domain matching may be weaker"
        ),
        (
            "selected Metalink parser options, but this wget2 build lacks +gpgme; "
            "signed Metalinks cannot be verified"
        ),
        (
            "selected signature verification, but this wget2 build lacks +gpgme; "
            "detached signatures cannot be verified"
        ),
        "selected internationalized hostnames, but this wget2 build lacks +idn2",
    ]


def test_site_dry_run_can_plan_without_wget2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda _name: None)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        dry_run=True,
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == "dry-run"
    assert result.ydl_opts is not None
    assert result.ydl_opts["backend"] == "wget2"


def test_site_engine_emits_wget_events(tmp_path: Path, monkeypatch) -> None:
    def fake_run_args_stream(args, *, on_line, timeout):
        on_line("index.html 50% [======>      ]")
        (tmp_path / "index.html").write_text("downloaded", encoding="utf-8")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget,
    )
    events: list[ProgressEvent] = []

    result = SiteMirrorEngine().mirror(options, progress_callback=events.append)

    assert result.status == DownloadStatus.success
    assert [event.status for event in events] == ["starting", "downloading", "done"]
    assert [event.engine for event in events] == [
        EngineKind.wget,
        EngineKind.wget,
        EngineKind.wget,
    ]


def test_site_engine_publishes_warc_atomically_and_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "mirror"

    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        prefix_arg = next(arg for arg in args if arg.startswith("--warc-file="))
        runtime_prefix = Path(prefix_arg.partition("=")[2])
        runtime_warc = runtime_prefix.with_name(f"{runtime_prefix.name}.warc.gz")
        runtime_warc.write_bytes(b"private WARC")
        runtime_warc.chmod(0o666)
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("downloaded", encoding="utf-8")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    requested = tmp_path / "capture.warc.gz"

    result = SiteMirrorEngine().mirror(
        SiteDownloadOptions(
            url="https://example.com/docs/",
            output_dir=output,
            backend=SiteBackendChoice.wget,
            warc_file=requested,
        )
    )

    assert result.status == DownloadStatus.success
    assert requested.read_bytes() == b"private WARC"
    assert stat.S_IMODE(requested.stat().st_mode) == 0o600
    assert not (tmp_path / "capture.warc.gz.warc.gz").exists()
    assert result.ydl_opts is not None
    assert result.ydl_opts["warc_outputs"] == [str(requested)]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_site_engine_refuses_warc_symlink_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside"
    target.write_bytes(b"preserve")
    requested = tmp_path / "capture.warc.gz"
    requested.symlink_to(target)
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    with pytest.raises(EngineError, match="unsafe WARC output"):
        SiteMirrorEngine().mirror(
            SiteDownloadOptions(
                url="https://example.com/docs/",
                output_dir=tmp_path / "mirror",
                backend=SiteBackendChoice.wget,
                warc_file=requested,
                overwrite=True,
            )
        )

    assert target.read_bytes() == b"preserve"


def test_site_engine_parses_wget2_stats(tmp_path: Path, monkeypatch) -> None:
    def fake_run_args_stream(args, *, on_line, timeout):
        _ = timeout
        on_line("index.html 100% [============>]")
        (tmp_path / "index.html").write_text("downloaded", encoding="utf-8")
        for arg in args:
            if arg.startswith("--stats-site=csv:"):
                Path(arg.split(":", 1)[1]).write_text(
                    "host,files\nexample.com,2\n",
                    encoding="utf-8",
                )
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.success
    assert result.ydl_opts is not None
    stats = result.ydl_opts["stats"]
    assert stats["site"]["rows"] == [{"host": "example.com", "files": "2"}]
    assert stats["summary"]["site"]["urls"] == 1


def test_site_engine_reports_wget2_stats_on_error(tmp_path: Path, monkeypatch) -> None:
    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        for arg in args:
            if arg.startswith("--stats-site=csv:"):
                Path(arg.split(":", 1)[1]).write_text(
                    "ID,ParentID,URL,Status,Link,Method,Size,SizeDecompressed,"
                    "TransferTime,ResponseTime,Encoding,Verification,Last-Modified,"
                    "Content-Type\n"
                    "1,0,https://example.com/ok.pdf,200,1,1,1024,1024,50,10,0,0,0,"
                    "application/pdf\n"
                    "2,0,https://example.com/missing.pdf,404,1,1,0,0,12,4,0,0,0,"
                    "text/html\n",
                    encoding="utf-8",
                )
        return SubprocessResult(args=list(args), returncode=8, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
    )

    with pytest.raises(EngineError) as raised:
        SiteMirrorEngine().mirror(options)

    message = str(raised.value)
    assert "wget2 exited 8" in message
    assert "downloaded 1.0 KB before exit" in message
    assert "1 failed URL" in message
    assert "404 https://example.com/missing.pdf" in message
    assert raised.value.stats["summary"]["site"]["failures"] == 1


def test_site_engine_rejects_zero_exit_with_failed_wget2_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        (tmp_path / "index.html").write_text("downloaded", encoding="utf-8")
        for arg in args:
            if arg.startswith("--stats-site=csv:"):
                Path(arg.split(":", 1)[1]).write_text(
                    "ID,ParentID,URL,Status,Link,Method,Size,SizeDecompressed,"
                    "TransferTime,ResponseTime,Encoding,Verification,Last-Modified,"
                    "Content-Type\n"
                    "1,0,https://example.com/docs/,200,1,1,32,32,1,1,0,0,0,text/html\n"
                    "2,1,https://example.com/docs/missing.css,404,1,1,0,0,1,1,0,0,0,"
                    "text/html\n",
                    encoding="utf-8",
                )
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
    )
    events: list[ProgressEvent] = []

    with pytest.raises(MirrorDownloadError, match="completed with failed requests") as raised:
        SiteMirrorEngine().mirror(options, progress_callback=events.append)

    assert [event.status for event in events] == ["starting", "error"]
    assert raised.value.stats["summary"]["site"]["failures"] == 1


def test_site_engine_exports_browser_cookies_for_wget2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[str] = []

    def fake_export(browser: str, directory: Path) -> Path:
        assert browser == "safari"
        path = directory / "browser-cookies.txt"
        path.write_text(".example.com\tTRUE\t/\tTRUE\t0\tsid\tsecret\n", encoding="utf-8")
        return path

    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        captured_args.extend(args)
        cookie_arg = next(arg for arg in args if arg.startswith("--load-cookies="))
        cookie_path = Path(cookie_arg.split("=", 1)[1])
        assert cookie_path.read_text(encoding="utf-8").endswith("sid\tsecret\n")
        (tmp_path / "index.html").write_text("downloaded", encoding="utf-8")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends._export_browser_cookies_to_file", fake_export)
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        browser_cookies="safari",
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.success
    assert any(arg.endswith("/browser-cookies.txt") for arg in captured_args)


def test_site_engine_prepares_saved_cookie_jar_as_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_jar = tmp_path / "saved-cookies.txt"
    cookie_jar.write_text("old", encoding="utf-8")
    cookie_jar.chmod(0o666)

    def fake_run_args_stream(args, *, on_line, timeout):
        _ = on_line, timeout
        assert f"--save-cookies={cookie_jar}" in args
        assert cookie_jar.stat().st_mode & 0o777 == 0o600
        (tmp_path / "index.html").write_text("downloaded", encoding="utf-8")
        return SubprocessResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.run_args_stream", fake_run_args_stream)
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        save_cookies=cookie_jar,
    )

    result = SiteMirrorEngine().mirror(options)

    assert result.status == DownloadStatus.success


def test_site_engine_rejects_browser_and_explicit_cookie_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    options = SiteDownloadOptions(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        backend=SiteBackendChoice.wget2,
        browser_cookies="safari",
        load_cookies=tmp_path / "cookies.txt",
    )

    with pytest.raises(EngineError, match="Use either --load-cookies or --cookies-from-browser"):
        SiteMirrorEngine().mirror(options)


def test_parse_wget2_stats_files_ignores_empty_files(tmp_path: Path) -> None:
    site = tmp_path / "site.csv"
    dns = tmp_path / "dns.csv"
    site.write_text("host,files\nexample.com,2\n", encoding="utf-8")
    dns.write_text("", encoding="utf-8")

    stats = parse_wget2_stats_files({"site": site, "dns": dns})

    assert stats["site"] == {"format": "csv", "rows": [{"host": "example.com", "files": "2"}]}
    assert stats["summary"]["site"]["urls"] == 1


def test_parse_wget2_stats_files_normalizes_headerless_site_and_server_stats(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site.csv"
    server = tmp_path / "server.csv"
    site.write_text(
        "1,0,https://example.com/index.html,200,1,1,1024,2048,50,10,0,0,1445412480,text/html\n"
        "2,1,https://example.com/missing.png,404,1,1,0,0,12,4,0,0,0,image/png\n",
        encoding="utf-8",
    )
    server.write_text("example.com,93.184.216.34,https,0,0,1,1\n", encoding="utf-8")

    stats = parse_wget2_stats_files({"site": site, "server": server})

    assert stats["site"]["rows"][0]["url"] == "https://example.com/index.html"
    assert stats["server"]["rows"][0]["hostname"] == "example.com"
    assert stats["summary"]["site"] == {
        "urls": 2,
        "status_counts": {"200": 1, "404": 1},
        "failures": 1,
        "redirects": 0,
        "downloaded_bytes": 1024,
        "decompressed_bytes": 2048,
        "transfer_time_ms": 62,
        "response_time_ms": 14,
        "mime_types": {"text/html": 1, "image/png": 1},
    }
    assert stats["summary"]["server"] == {
        "hosts": 1,
        "schemes": {"https": 1},
        "hsts_hosts": 1,
        "csp_hosts": 1,
        "https_hosts": 1,
        "http_hosts": 0,
        "hosts_without_hsts": [],
        "hosts_without_csp": [],
        "mixed_scheme_hosts": [],
    }


def test_parse_wget2_stats_files_summarizes_headered_site_stats(tmp_path: Path) -> None:
    site = tmp_path / "site.csv"
    site.write_text(
        "ID,ParentID,URL,Status,Link,Method,Size,SizeDecompressed,TransferTime,"
        "ResponseTime,Encoding,Verification,Last-Modified,Content-Type\n"
        "1,0,http://pdf.textfiles.com/robots.txt,404,1,1,196,196,114,114,0,0,0,"
        "text/html\n"
        "2,0,http://pdf.textfiles.com/cutouts/,200,1,1,2480,2480,104,104,0,0,"
        "1220828706,text/html\n"
        "3,2,http://pdf.textfiles.com/cutouts/eMac.pdf,200,1,1,312980,312980,1106,"
        "106,0,0,1086248807,application/pdf\n",
        encoding="utf-8",
    )

    stats = parse_wget2_stats_files({"site": site})

    assert stats["summary"]["site"] == {
        "urls": 3,
        "status_counts": {"404": 1, "200": 2},
        "failures": 1,
        "redirects": 0,
        "downloaded_bytes": 315_656,
        "decompressed_bytes": 315_656,
        "transfer_time_ms": 1_324,
        "response_time_ms": 324,
        "mime_types": {"text/html": 2, "application/pdf": 1},
    }


def test_wget2_failed_urls_can_ignore_an_absent_robots_policy_file() -> None:
    stats = {
        "site": {
            "rows": [
                {"status": 404, "url": "https://example.com/robots.txt"},
                {"status": 404, "url": "https://example.com/missing.css"},
            ]
        }
    }

    assert _wget2_failed_urls(stats, ignore_missing_robots=True) == [
        "404 https://example.com/missing.css"
    ]


def test_parse_wget2_stats_files_summarizes_dns_tls_and_ocsp(tmp_path: Path) -> None:
    dns = tmp_path / "dns.csv"
    tls = tmp_path / "tls.csv"
    ocsp = tmp_path / "ocsp.csv"
    dns.write_text(
        "example.com,93.184.216.34,443,20\n"
        "example.com,,443,50\n"
        "static.example.com,2606:2800:220:1:248:1893:25c8:1946,443,30\n",
        encoding="utf-8",
    )
    tls.write_text(
        "example.com,5,1,0,1,h2,2,3,40\nstatic.example.com,4,0,1,0,http/1.1,1,2,20\n",
        encoding="utf-8",
    )
    ocsp.write_text(
        "example.com,1,2,0,1\nstatic.example.com,0,1,1,0\n",
        encoding="utf-8",
    )

    stats = parse_wget2_stats_files({"dns": dns, "tls": tls, "ocsp": ocsp})

    assert stats["dns"]["rows"][0]["dns_secs"] == "20"
    assert stats["tls"]["rows"][0]["version"] == "5"
    assert stats["ocsp"]["rows"][0]["nvalid"] == "2"
    assert stats["summary"]["dns"] == {
        "lookups": 3,
        "hosts": 2,
        "addresses": 2,
        "ipv4_addresses": 1,
        "ipv6_addresses": 1,
        "ports": [443],
        "failures": 1,
        "lookup_time_ms": 100,
        "max_lookup_time_ms": 50,
        "average_lookup_time_ms": 33,
    }
    assert stats["summary"]["tls"] == {
        "connections": 2,
        "versions": {"TLS1.3": 1, "TLS1.2": 1},
        "false_start_connections": 1,
        "tfo_connections": 1,
        "resumed_connections": 1,
        "alpn_protocols": {"h2": 1, "http/1.1": 1},
        "http_protocols": {"HTTP/2": 1, "HTTP/1.1": 1},
        "max_cert_chain_size": 3,
        "tls_time_ms": 60,
        "max_tls_time_ms": 40,
        "average_tls_time_ms": 30,
    }
    assert stats["summary"]["ocsp"] == {
        "hosts": 2,
        "stapled_hosts": 1,
        "valid_responses": 3,
        "revoked_responses": 1,
        "ignored_responses": 1,
        "revoked_hosts": ["static.example.com"],
        "ignored_hosts": ["example.com"],
    }


def test_parse_wget2_server_stats_reports_missing_security_state(tmp_path: Path) -> None:
    server = tmp_path / "server.csv"
    server.write_text(
        "example.com,93.184.216.34,http,0,0,0,0\n"
        "example.com,93.184.216.34,https,0,0,0,1\n"
        "static.example.com,93.184.216.35,https,0,0,1,0\n",
        encoding="utf-8",
    )

    stats = parse_wget2_stats_files({"server": server})

    assert stats["summary"]["server"] == {
        "hosts": 2,
        "schemes": {"http": 1, "https": 2},
        "hsts_hosts": 1,
        "csp_hosts": 1,
        "https_hosts": 2,
        "http_hosts": 1,
        "hosts_without_hsts": ["example.com"],
        "hosts_without_csp": ["static.example.com"],
        "mixed_scheme_hosts": ["example.com"],
    }
