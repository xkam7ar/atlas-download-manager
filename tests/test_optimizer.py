from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.errors import AtlasError
from atlas.hub import EngineRouter
from atlas.models import (
    AudioCodec,
    DirectFileProbe,
    EngineKind,
    FileBackendChoice,
    HubKind,
    HubRequest,
    SiteBackendChoice,
    VideoCodecChoice,
    WorkItem,
)
from atlas.optimizer import DownloadOptimizer, plan_as_dict


def test_optimizer_builds_media_preview(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://www.youtube.com/watch?v=abc",
        output_dir=tmp_path,
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)

    plan = DownloadOptimizer(settings).optimize(request, route)
    data = plan_as_dict(plan.preview)

    assert route.kind == HubKind.video
    assert route.engine == EngineKind.ytdlp
    assert data["summary"]["format"] == "bestvideo*+bestaudio/best"
    assert data["summary"]["video_codec"] == "auto"
    assert data["summary"]["noplaylist"] is True
    assert data["session"]["session_type"] == "single_video"
    assert data["session"]["scheduler_policy"]["mode"] == "adaptive"
    assert data["session"]["manifest"][0]["selected_backend"] == "yt-dlp"


def test_optimizer_applies_hub_video_codec(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://www.youtube.com/watch?v=abc",
        output_dir=tmp_path,
        requested_kind=HubKind.video,
        dry_run=True,
        video_codec=VideoCodecChoice.vp9,
    )
    route = EngineRouter(settings).route(request)

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.preview.summary["video_codec"] == "vp9"
    assert "[vcodec^=vp9]" in str(plan.preview.summary["format"])


def test_optimizer_applies_hub_audio_codec(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://www.youtube.com/watch?v=abc",
        output_dir=tmp_path,
        requested_kind=HubKind.audio,
        dry_run=True,
        audio_codec=AudioCodec.mp3,
        audio_quality=3,
    )
    route = EngineRouter(settings).route(request)

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.preview.summary["codec"] == "mp3"
    assert plan.preview.summary["audio_quality"] == 3
    assert plan.options.codec == AudioCodec.mp3
    assert plan.preview.session is not None
    assert plan.preview.session.intent == "audio"


def test_optimizer_marks_explicit_playlist_as_media_session(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://www.youtube.com/playlist?list=PL123",
        output_dir=tmp_path,
        requested_kind=HubKind.audio,
        audio_codec=AudioCodec.opus,
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)
    options = DownloadOptimizer(settings)._audio_options(request).model_copy(
        update={"playlist": True}
    )

    plan = DownloadOptimizer(settings).optimize_options(route, options)

    assert plan.preview.session is not None
    assert plan.preview.session.session_type == "media_playlist"
    assert plan.preview.session.scheduler_policy["max_active_media"] == 2


def test_optimizer_builds_file_preview_with_checksum(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend="native",
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)

    plan = DownloadOptimizer(settings).optimize(
        request,
        route,
        backend="native",
        checksum="sha256:" + "0" * 64,
    )

    assert route.kind == HubKind.file
    assert plan.preview.summary["backend"] == "native"
    assert plan.preview.summary["checksum"] == "sha256:" + "0" * 64
    assert plan.preview.session is not None
    assert plan.preview.session.session_type == "direct_file"
    assert plan.preview.session.customization["checksum"] is True


def test_optimizer_builds_wget2_file_preview(tmp_path: Path, monkeypatch) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
        backend="wget2",
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")

    plan = DownloadOptimizer(settings).optimize(request, route, backend="wget2")

    assert plan.route.engine == EngineKind.wget2
    assert plan.preview.summary["backend"] == "wget2"
    assert plan.preview.summary["backend_reason"] == "user selected wget2"
    assert "/opt/bin/wget2" in plan.preview.args
    assert "--output-document" in plan.preview.args


def test_optimizer_treats_metalink_as_manifest_plan(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/releases/app.meta4",
        output_dir=tmp_path,
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert route.kind == HubKind.manifest
    assert route.engine == EngineKind.aria2
    assert plan.preview.summary["backend"] == "aria2"
    assert plan.preview.summary["force_metalink"] is True
    assert plan.preview.summary["probe"]["reason"] == "metalink manifest"


def test_optimizer_builds_directory_mirror_preview(tmp_path: Path, monkeypatch) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        dir_depth=3,
        dir_backend=SiteBackendChoice.wget,
    )
    request = HubRequest(
        url="https://example.com/files/",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.route.kind == HubKind.dir
    assert plan.route.engine == EngineKind.wget
    assert plan.preview.summary["mirror_kind"] == "dir"
    assert plan.preview.summary["depth"] == 3
    assert plan.preview.summary["assets"] is False
    assert plan.preview.summary["convert_links"] is False
    assert plan.preview.summary["no_parent"] is True
    assert plan.preview.session is not None
    assert plan.preview.session.session_type == "directory_session"
    assert plan.preview.session.customization["depth"] == 3


def test_optimizer_rejects_adaptive_mirror_over_max_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        requested_kind=HubKind.site,
        explain=True,
    )
    route = EngineRouter(settings).route(request)
    options = DownloadOptimizer(settings)._site_options(request, "wget2").model_copy(
        update={"max_files": 3}
    )
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.site,
            scan_counts={"same_host": 4},
        ),
    )

    with pytest.raises(AtlasError, match="exceeding --max-files"):
        DownloadOptimizer(settings).optimize_options(route, options)


def test_optimizer_rejects_adaptive_mirror_over_estimated_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/docs/",
        output_dir=tmp_path,
        requested_kind=HubKind.site,
        explain=True,
    )
    route = EngineRouter(settings).route(request)
    options = DownloadOptimizer(settings)._site_options(request, "wget2").model_copy(
        update={"max_total_size": "1M"}
    )
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.site,
            scan_estimated_bytes=2 * 1024 * 1024,
        ),
    )

    with pytest.raises(AtlasError, match="exceeding --max-total-size"):
        DownloadOptimizer(settings).optimize_options(route, options)


def test_optimizer_chooses_aria2_for_large_ranged_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(url="https://example.com/app.dmg", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=512 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".dmg",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.route.engine == EngineKind.aria2
    assert plan.preview.summary["backend"] == "aria2"
    assert plan.preview.summary["backend_reason"] == "large file with range support"
    assert plan.preview.summary["probe"]["content_length"] == 512 * 1024 * 1024
    assert plan.preview.session is not None
    assert plan.preview.session.scheduler_policy["backend"] == "aria2"


def test_optimizer_without_adaptive_preserves_configured_threads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        aria2_connections=12,
        aria2_splits=10,
    )
    request = HubRequest(url="https://example.com/app.dmg", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=512 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".dmg",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.preview.summary["connections"] == 12
    assert plan.preview.summary["splits"] == 10
    assert plan.preview.summary["adaptive"] is None


def test_optimizer_chooses_native_for_small_file_and_probe_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(url="https://example.com/download", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=1024 * 1024,
            filename="Installer.dmg",
            supports_ranges=True,
            file_extension=".dmg",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.route.engine == EngineKind.native
    assert plan.preview.summary["backend"] == "native"
    assert plan.preview.summary["backend_reason"] == "small file"
    assert plan.preview.output == tmp_path / "Installer.dmg"


def test_optimizer_upgrades_http_link_metalink_to_aria2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(url="https://example.com/releases/app.tar.gz", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=1024 * 1024,
            file_extension=".gz",
            metalink_url="https://example.com/releases/app.meta4",
            metalink_source="describedby",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.route.engine == EngineKind.aria2
    assert plan.options.url == "https://example.com/releases/app.meta4"
    assert plan.preview.summary["backend"] == "aria2"
    assert plan.preview.summary["backend_reason"] == "HTTP Link rel=describedby Metalink"
    assert plan.preview.summary["force_metalink"] is True
    assert plan.preview.summary["probe"]["metalink_url"] == "https://example.com/releases/app.meta4"


def test_optimizer_keeps_explicit_wget2_for_http_link_metalink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/releases/app.tar.gz",
        output_dir=tmp_path,
        backend="wget2",
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=1024 * 1024,
            file_extension=".gz",
            metalink_url="https://example.com/releases/app.meta4",
            metalink_source="describedby",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route, backend="wget2")

    assert plan.route.engine == EngineKind.wget2
    assert plan.options.backend == FileBackendChoice.wget2
    assert plan.options.url == "https://example.com/releases/app.tar.gz"
    assert plan.preview.summary["backend"] == "wget2"
    assert plan.preview.summary["backend_reason"] == "user selected wget2"
    assert plan.preview.summary["force_metalink"] is False


def test_optimizer_uses_redirect_filename_when_trusted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        file_content_disposition=False,
        file_trust_server_names=True,
    )
    request = HubRequest(url="https://example.com/download", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url="https://cdn.example.com/releases/App.dmg?token=1",
            redirected=True,
            content_length=1024 * 1024,
            filename="ignored-by-policy.dmg",
            supports_ranges=True,
            file_extension=".dmg",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.preview.output == tmp_path / "App.dmg"
    assert plan.options.probe is not None


def test_optimizer_chooses_native_when_aria2_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(url="https://example.com/app.dmg", output_dir=tmp_path)
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=512 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".dmg",
        ),
    )

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.route.engine == EngineKind.native
    assert plan.preview.summary["backend"] == "native"
    assert plan.preview.summary["backend_reason"] == "aria2c not installed"


def test_optimizer_dry_run_skips_file_probe(tmp_path: Path, monkeypatch) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://example.com/app.dmg",
        output_dir=tmp_path,
        dry_run=True,
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")

    def fail_probe(_url: str) -> DirectFileProbe:
        raise AssertionError("dry-run should not probe the network")

    monkeypatch.setattr("atlas.optimizer.probe_direct_file", fail_probe)

    plan = DownloadOptimizer(settings).optimize(request, route)

    assert plan.preview.summary["backend"] == "aria2"
    assert plan.preview.summary["probe"]["probed"] is False
    assert plan.preview.summary["probe"]["error"] == "dry run: probe skipped"
