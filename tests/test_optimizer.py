from __future__ import annotations

from pathlib import Path

import pytest

import atlas.optimizer as optimizer_module
from atlas.config import AtlasSettings
from atlas.errors import AtlasError
from atlas.hub import EngineRouter
from atlas.models import (
    AudioCodec,
    DirectFileProbe,
    DirectoryMirrorOptions,
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


def test_router_treats_youtube_nocookie_as_media(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://www.youtube-nocookie.com/embed/abc123",
        output_dir=tmp_path,
    )

    route = EngineRouter(settings).route(request)

    assert route.kind == HubKind.video
    assert route.engine == EngineKind.ytdlp
    assert route.is_media_host is True


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
    options = (
        DownloadOptimizer(settings)._audio_options(request).model_copy(update={"playlist": True})
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


def test_optimizer_uses_exact_file_plan_for_supported_text_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://files.example/root/",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
    )
    route = EngineRouter(settings).route(request)
    root = WorkItem(
        url=request.url,
        final_url=request.url,
        host="files.example",
        final_host="files.example",
        kind=HubKind.site,
        scan_type="directory-style text index",
        scan_counts={"complete": 1, "same_host": 2},
        discovered_work_items=[
            WorkItem(
                url="https://files.example/root/README.md",
                host="files.example",
                final_host="files.example",
                kind=HubKind.file,
                content_length=638,
            ),
            WorkItem(
                url="https://files.example/root/archive/",
                host="files.example",
                final_host="files.example",
                kind=HubKind.dir,
            ),
        ],
    )
    monkeypatch.setattr("atlas.optimizer.scan_site", lambda *_args, **_kwargs: root)
    options = DirectoryMirrorOptions(
        url=request.url,
        output_dir=tmp_path,
        depth=1,
        accept="README.md",
        max_files=1,
        max_total_size="1K",
    )

    plan = DownloadOptimizer(settings).optimize_options(route, options)

    assert isinstance(plan.options, DirectoryMirrorOptions)
    assert plan.options.exact_directory_index is True
    assert [item.filename for item in plan.options.exact_directory_items] == ["README.md"]
    assert plan.preview.summary["backend"] == "native-exact-index"


def test_exact_directory_suffix_filters_match_bare_and_dotted_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://files.example/root/",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
    )
    route = EngineRouter(settings).route(request)
    root = WorkItem(
        url=request.url,
        final_url=request.url,
        kind=HubKind.site,
        scan_type="directory-style text index",
        scan_counts={"complete": 1, "same_host": 3},
        discovered_work_items=[
            WorkItem(url=f"{request.url}book.pdf", kind=HubKind.file, content_length=10),
            WorkItem(url=f"{request.url}cover.jpg", kind=HubKind.file, content_length=20),
            WorkItem(url=f"{request.url}notes.txt", kind=HubKind.file, content_length=30),
        ],
    )
    monkeypatch.setattr("atlas.optimizer.scan_site", lambda *_args, **_kwargs: root)
    options = DirectoryMirrorOptions(
        url=request.url,
        output_dir=tmp_path,
        depth=1,
        accept="pdf,.jpg",
    )

    plan = DownloadOptimizer(settings).optimize_options(route, options)

    assert isinstance(plan.options, DirectoryMirrorOptions)
    assert plan.options.exact_directory_base_url == request.url
    assert [item.filename for item in plan.options.exact_directory_items] == [
        "book.pdf",
        "cover.jpg",
    ]


def test_exact_directory_scan_has_hard_page_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(optimizer_module, "_EXACT_DIRECTORY_MAX_PAGES", 2)
    seed = WorkItem(
        url="https://files.example/root/",
        final_url="https://files.example/root/",
        kind=HubKind.site,
        scan_type="directory-style text index",
        scan_counts={"complete": 1},
        discovered_work_items=[
            WorkItem(url="https://files.example/root/one/", kind=HubKind.dir),
            WorkItem(url="https://files.example/root/two/", kind=HubKind.dir),
        ],
    )
    options = DirectoryMirrorOptions(
        url=seed.url,
        output_dir=tmp_path,
        depth=2,
    )

    with pytest.raises(AtlasError, match="more than 2 directory pages"):
        optimizer_module._collect_exact_directory_items(options, seed)


def test_exact_directory_planning_consumes_shared_runtime_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 10.0}
    seed = WorkItem(
        url="https://files.example/root/",
        final_url="https://files.example/root/",
        kind=HubKind.dir,
        scan_type="directory-style text index",
        scan_counts={"complete": 1},
        discovered_work_items=[
            WorkItem(
                url="https://files.example/root/book.pdf",
                kind=HubKind.file,
                filename="book.pdf",
                content_length=4,
            )
        ],
    )

    def fake_scan(url: str, *, dry_run: bool, timeout: float) -> WorkItem:
        assert url == seed.url
        assert dry_run is False
        assert timeout == 10.0
        clock["now"] = 12.5
        return seed

    monkeypatch.setattr("atlas.optimizer.monotonic", lambda: clock["now"])
    monkeypatch.setattr("atlas.optimizer.scan_site", fake_scan)
    options = DirectoryMirrorOptions(
        url=seed.url,
        output_dir=tmp_path,
        max_runtime=10,
    )

    optimized = DownloadOptimizer(AtlasSettings(output_dir=tmp_path))._optimize_site_options(
        options
    )

    assert optimized.exact_directory_index is True
    assert optimized.planning_runtime_seconds == 2.5


def test_directory_relative_name_canonicalizes_default_port_and_idna() -> None:
    relative = optimizer_module._directory_relative_name(
        "https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example/root/",
        "https://xn--bcher-kva.example:443/root/book.pdf",
    )

    assert relative == "book.pdf"


def test_directory_relative_name_rejects_encoded_escape_and_preserves_percent() -> None:
    base = "https://files.example/root/"

    with pytest.raises(AtlasError, match="unsafe path component"):
        optimizer_module._directory_relative_name(
            base,
            f"{base}%252e%252e/secret.bin",
        )

    assert (
        optimizer_module._directory_relative_name(base, f"{base}100%25-free.txt") == "100%-free.txt"
    )


@pytest.mark.parametrize(
    "item_url",
    [
        "http://files.example/root/book.pdf",
        "https://files.example:8443/root/book.pdf",
    ],
)
def test_directory_relative_name_rejects_scheme_or_port_changes(item_url: str) -> None:
    with pytest.raises(AtlasError, match="escape to another host"):
        optimizer_module._directory_relative_name(
            "https://files.example/root/",
            item_url,
        )


def test_directory_scan_limits_apply_after_file_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://files.example/root/",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
        explain=True,
    )
    route = EngineRouter(settings).route(request)
    scan = WorkItem(
        url=request.url,
        final_url=request.url,
        kind=HubKind.site,
        scan_type="directory-style text index",
        scan_counts={"same_host": 12, "complete": 1},
        scan_estimated_bytes=10 * 1024 * 1024,
        discovered_work_items=[
            *[
                WorkItem(
                    url=f"https://files.example/root/folder-{index}/",
                    kind=HubKind.dir,
                )
                for index in range(10)
            ],
            WorkItem(
                url="https://files.example/root/song.mid",
                kind=HubKind.file,
                content_length=4_934,
            ),
            WorkItem(
                url="https://files.example/root/book.pdf",
                kind=HubKind.file,
                content_length=2 * 1024 * 1024,
            ),
        ],
    )
    monkeypatch.setattr("atlas.optimizer.scan_site", lambda *_args, **_kwargs: scan)
    options = DirectoryMirrorOptions(
        url=request.url,
        output_dir=tmp_path,
        accept="song.mid",
        max_files=1,
        max_total_size="8K",
        explain=True,
    )

    plan = DownloadOptimizer(settings).optimize_options(route, options)

    assert plan.options.adaptive_plan is not None
    assert plan.options.exact_directory_index is True


def test_directory_execution_uses_same_origin_final_scan_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://files.example/root",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda *_args, **_kwargs: WorkItem(
            url=request.url,
            final_url="https://files.example/root/",
            kind=HubKind.site,
            scan_type="directory-style HTML index",
        ),
    )

    plan = DownloadOptimizer(settings).optimize_options(
        route,
        DirectoryMirrorOptions(url=request.url, output_dir=tmp_path),
    )

    assert plan.options.url == "https://files.example/root/"


def test_directory_rejects_cross_origin_seed_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    request = HubRequest(
        url="https://files.example/root/",
        output_dir=tmp_path,
        requested_kind=HubKind.dir,
    )
    route = EngineRouter(settings).route(request)
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda *_args, **_kwargs: WorkItem(
            url=request.url,
            final_url="https://cdn.example/root/",
            kind=HubKind.site,
            scan_type="directory-style HTML index",
        ),
    )

    with pytest.raises(AtlasError, match="redirected outside the requested origin"):
        DownloadOptimizer(settings).optimize_options(
            route,
            DirectoryMirrorOptions(url=request.url, output_dir=tmp_path),
        )


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
    options = (
        DownloadOptimizer(settings)
        ._site_options(request, "wget2")
        .model_copy(update={"max_files": 3})
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
    options = (
        DownloadOptimizer(settings)
        ._site_options(request, "wget2")
        .model_copy(update={"max_total_size": "1M"})
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
