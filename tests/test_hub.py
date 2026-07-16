from __future__ import annotations

from atlas.hub import route_url
from atlas.models import HubKind


def test_route_url_detects_media_hosts() -> None:
    decision = route_url("https://www.youtube.com/watch?v=abc")

    assert decision.kind == HubKind.video
    assert decision.reason == "media host"


def test_route_url_detects_obvious_files() -> None:
    decision = route_url("https://example.com/releases/app.dmg")

    assert decision.kind == HubKind.file
    assert "file extension" in decision.reason


def test_route_url_detects_metalink_manifests() -> None:
    meta4 = route_url("https://example.com/releases/app.meta4")
    metalink = route_url("https://example.com/releases/app.metalink")

    assert meta4.kind == HubKind.manifest
    assert meta4.reason == "metalink manifest .meta4"
    assert metalink.kind == HubKind.manifest
    assert metalink.reason == "metalink manifest .metalink"


def test_route_url_keeps_site_mirroring_explicit() -> None:
    decision = route_url("https://example.com/docs/")

    assert decision.kind == HubKind.file
    assert "safe default" in decision.reason


def test_route_url_honors_requested_kind() -> None:
    decision = route_url("https://example.com/docs/", HubKind.site)

    assert decision.kind == HubKind.site
    assert decision.reason == "user selected site"


def test_route_url_honors_requested_directory_kind() -> None:
    decision = route_url("https://example.com/files/", HubKind.dir)

    assert decision.kind == HubKind.dir
    assert decision.reason == "user selected dir"
