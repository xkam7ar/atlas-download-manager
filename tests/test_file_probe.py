from __future__ import annotations

from atlas.file_probe import probe_direct_file, unprobed_direct_file, url_fingerprint
from atlas.network import FetchError, FetchErrorCode, FetchFailure, FetchResponse


def _fetch_response(
    url: str,
    headers: dict[str, str],
    *,
    status: int = 200,
) -> FetchResponse:
    return FetchResponse(
        url=url,
        final_url=url,
        status_code=status,
        headers=headers,
        body=b"",
    )


def test_probe_direct_file_reads_head_metadata(monkeypatch) -> None:
    def fake_request(_self, url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            "https://cdn.example.com/app.dmg",
            {
                "Content-Type": "application/x-apple-diskimage",
                "Content-Length": "734003200",
                "Content-Disposition": 'attachment; filename="Installer.dmg"',
                "Accept-Ranges": "bytes",
                "ETag": '"abc"',
                "Last-Modified": "Sun, 07 Jun 2026 10:00:00 GMT",
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/download")

    assert probe.probed is True
    assert probe.final_url == "https://cdn.example.com/app.dmg"
    assert probe.redirected is True
    assert probe.content_type == "application/x-apple-diskimage"
    assert probe.content_length == 734003200
    assert probe.filename == "Installer.dmg"
    assert probe.accept_ranges == "bytes"
    assert probe.supports_ranges is True
    assert probe.etag == '"abc"'
    assert probe.last_modified == "Sun, 07 Jun 2026 10:00:00 GMT"
    assert probe.file_extension == ".dmg"


def test_probe_direct_file_sanitizes_hostile_content_disposition(monkeypatch) -> None:
    def fake_request(_self, url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            url,
            {
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''..%2F..%2FSecret%0AReport%5Cfinal%3F.pdf"
                ),
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/download")

    assert probe.filename == "Secret_Report_final_.pdf"
    assert probe.file_extension == ".pdf"


def test_probe_direct_file_falls_back_to_range_get(monkeypatch) -> None:
    def fake_request(_self, url, _options, *, method, extra_headers=None, **_kwargs):
        if method == "HEAD":
            raise FetchError(
                FetchFailure(
                    code=FetchErrorCode.http_error,
                    message="HTTP 405: Method Not Allowed",
                    url=url,
                    status_code=405,
                )
            )
        assert method == "GET"
        assert extra_headers == {"Range": "bytes=0-0"}
        return _fetch_response(
            url,
            {
                "Content-Range": "bytes 0-0/104857600",
                "Content-Type": "application/octet-stream",
            },
            status=206,
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/archive.zip")

    assert probe.content_length == 104857600
    assert probe.supports_ranges is True
    assert probe.file_extension == ".zip"


def test_probe_direct_file_discovers_http_link_metalink(monkeypatch) -> None:
    def fake_request(_self, _url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            "https://example.com/releases/app.tar.gz",
            {
                "Content-Type": "application/gzip",
                "Link": (
                    '</releases/app.meta4>; rel="describedby"; '
                    'type="application/metalink4+xml", '
                    '</mirrors/app.tar.gz>; rel="duplicate"'
                ),
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/releases/app.tar.gz")

    assert probe.metalink_url == "https://example.com/releases/app.meta4"
    assert probe.metalink_source == "describedby"


def test_probe_direct_file_explains_page_url_resolving_to_zip(monkeypatch) -> None:
    def fake_request(_self, _url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            "https://example.com/download?id=123",
            {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="release.zip"',
                "Content-Length": "42",
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/download?id=123")

    assert probe.file_extension == ".zip"
    assert "This looked like a page, but resolved to a ZIP." in probe.classification_notes
    assert (
        "No extension in URL, but Content-Disposition named release.zip."
        in probe.classification_notes
    )
    assert probe.mirror_fingerprint == "file:release.zip:42:?"


def test_probe_direct_file_explains_file_url_returning_html(monkeypatch) -> None:
    def fake_request(_self, url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            url,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": "1024",
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file("https://example.com/archive.zip")

    assert "This looked like a file, but returned HTML." in probe.classification_notes


def test_probe_direct_file_notes_http_https_shortlink_and_signed_url(monkeypatch) -> None:
    def fake_request(_self, _url, _options, *, method, **_kwargs):
        assert method == "HEAD"
        return _fetch_response(
            "https://downloads.example.com/archive.zip?X-Amz-Expires=60&X-Amz-Signature=abc",
            {
                "Content-Type": "application/zip",
                "Content-Length": "1024",
            },
        )

    monkeypatch.setattr("atlas.file_probe.FetchClient.request", fake_request)

    probe = probe_direct_file(
        "http://bit.ly/a?utm_source=newsletter&X-Amz-Signature=abc&X-Amz-Expires=60"
    )

    assert "Redirected from HTTP to HTTPS." in probe.classification_notes
    assert "Shortlink resolved before planning." in probe.classification_notes
    assert "tracking_params" in probe.warning_flags
    assert "signed_query_params" in probe.warning_flags
    assert "X-Amz-Signature=abc" in probe.url_fingerprint
    assert "utm_source" not in probe.url_fingerprint


def test_url_fingerprint_ignores_tracking_but_keeps_signed_params() -> None:
    fingerprint = url_fingerprint(
        "https://cdn.example.com/file.bin?utm_campaign=launch&X-Amz-Signature=abc&b=2"
    )

    assert fingerprint == "https://cdn.example.com/file.bin?X-Amz-Signature=abc&b=2"


def test_unprobed_direct_file_marks_reason() -> None:
    probe = unprobed_direct_file("https://example.com/archive.zip", reason="dry run")

    assert probe.probed is False
    assert probe.error == "dry run"
    assert probe.file_extension == ".zip"
