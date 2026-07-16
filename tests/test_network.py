from __future__ import annotations

import gzip
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request

import pytest

from atlas.network import (
    FetchClient,
    FetchError,
    FetchOptions,
    FetchResponse,
    _fetch_with_curl,
    _ValidatingRedirectHandler,
    open_request,
    redirect_safe_request,
)
from atlas.runner import SubprocessResult


class _Response:
    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self._body = BytesIO(body)
        self.status = 200
        self.headers = headers or {
            "Content-Length": str(len(body)),
            "Content-Type": "text/plain",
        }

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return "https://example.com/files/"

    def read(self, size: int) -> bytes:
        return self._body.read(size)


def test_fetch_client_closes_http_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = BytesIO(b"service unavailable")
    error = HTTPError(
        "https://example.com/files/",
        503,
        "Service Unavailable",
        {},
        response_body,
    )

    def raise_http_error(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("atlas.network.open_request", raise_http_error)

    with pytest.raises(FetchError, match="HTTP 503: Service Unavailable"):
        FetchClient().get("https://example.com/files/")

    assert response_body.closed


def test_fetch_response_normalizes_case_insensitive_header_names() -> None:
    response = FetchResponse(
        url="https://example.com/files/",
        final_url="https://example.com/files/",
        status_code=200,
        headers={
            "content-type": "text/html; charset=utf-8",
            "CONTENT-LENGTH": "42",
            "eTaG": '"abc"',
        },
        body=b"",
    )

    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Content-Length"] == "42"
    assert response.headers["ETag"] == '"abc"'


def test_redirect_safe_request_keeps_credentials_on_the_initial_hop_only() -> None:
    request = redirect_safe_request(
        "https://example.com/files/",
        headers={
            "User-Agent": "atlas-test",
            "Authorization": "Bearer secret",
            "Authentication": "private scheme secret",
            "X-Auth": "secret",
            "X-Api-Key": "secret",
            "Referer": "https://private.example/account",
            "X-Callback": "https://api.example/callback?token=secret",
        },
        method="GET",
    )

    assert request.headers["User-agent"] == "atlas-test"
    assert "Authorization" not in request.headers
    assert "Authentication" not in request.headers
    assert "X-auth" not in request.headers
    assert "X-api-key" not in request.headers
    assert "Referer" not in request.headers
    assert "X-callback" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Bearer secret"
    assert request.unredirected_hdrs["Authentication"] == "private scheme secret"
    assert request.unredirected_hdrs["X-auth"] == "secret"
    assert request.unredirected_hdrs["X-api-key"] == "secret"
    assert request.unredirected_hdrs["Referer"] == "https://private.example/account"
    assert request.unredirected_hdrs["X-callback"].endswith("?token=secret")


def test_fetch_client_uses_explicit_proxy_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_handlers: list[object] = []
    captured_requests: list[object] = []

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> _Response:
            assert timeout == 5
            captured_requests.append(request)
            return _Response(b"proxied")

    def fake_build_opener(*handlers: object) -> FakeOpener:
        captured_handlers.extend(handlers)
        return FakeOpener()

    monkeypatch.setattr("atlas.network.build_opener", fake_build_opener)

    response = FetchClient().get(
        "https://unresolvable.invalid/file",
        FetchOptions(timeout=5, proxy="http://proxy.example:8080"),
    )

    proxy_handler = next(
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    )
    assert proxy_handler.proxies == {}
    request = captured_requests[0]
    assert request.host == "proxy.example:8080"
    assert request._tunnel_host == "unresolvable.invalid"
    assert response.body == b"proxied"


def test_open_request_loads_netscape_cookie_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t2147483647\tsession\tprivate\n",
        encoding="utf-8",
    )
    captured_handlers: list[object] = []

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> _Response:
            assert request is not None
            assert timeout == 5
            return _Response(b"cookie response")

    def fake_build_opener(*handlers: object) -> FakeOpener:
        captured_handlers.extend(handlers)
        return FakeOpener()

    monkeypatch.setattr("atlas.network.build_opener", fake_build_opener)

    response = open_request(
        Request("https://example.com/private"),
        timeout=5,
        cookie_file=cookie_file,
    )

    cookie_handler = next(
        handler for handler in captured_handlers if isinstance(handler, HTTPCookieProcessor)
    )
    assert [(cookie.name, cookie.value) for cookie in cookie_handler.cookiejar] == [
        ("session", "private")
    ]
    assert response.read(100) == b"cookie response"


def test_redirect_handler_validates_target_before_following() -> None:
    checked: list[str] = []

    def reject(url: str) -> None:
        checked.append(url)
        raise ValueError("outside scope")

    handler = _ValidatingRedirectHandler(reject)

    with pytest.raises(ValueError, match="outside scope"):
        handler.redirect_request(
            Request("https://example.com/start"),
            BytesIO(),
            302,
            "Found",
            {},
            "https://outside.example/then-back",
        )

    assert checked == ["https://outside.example/then-back"]


@pytest.mark.parametrize(
    ("body", "expected", "truncated"),
    [
        (b"abcde", b"abcde", False),
        (b"abcdef", b"abcde", True),
    ],
)
def test_fetch_client_exposes_body_truncation(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    expected: bytes,
    truncated: bool,
) -> None:
    monkeypatch.setattr("atlas.network.urlopen", lambda *_args, **_kwargs: _Response(body))

    response = FetchClient().request("https://example.com/files/", body_limit=5)

    assert response.body == expected
    assert response.body_truncated is truncated


def test_fetch_client_decodes_unexpected_gzip_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'<a href="child/">child/</a>'
    encoded = gzip.compress(body)
    monkeypatch.setattr(
        "atlas.network.urlopen",
        lambda *_args, **_kwargs: _Response(
            encoded,
            headers={
                "Content-Length": str(len(encoded)),
                "Content-Type": "text/html",
                "Content-Encoding": "gzip",
            },
        ),
    )

    response = FetchClient().request("https://example.com/files/", body_limit=512)

    assert response.body == body
    assert response.body_truncated is False


def test_fetch_client_bounds_decompressed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = gzip.compress(b"a" * 1_000)
    monkeypatch.setattr(
        "atlas.network.urlopen",
        lambda *_args, **_kwargs: _Response(
            encoded,
            headers={"Content-Encoding": "gzip"},
        ),
    )

    response = FetchClient().request("https://example.com/files/", body_limit=32)

    assert response.body == b"a" * 32
    assert response.body_truncated is True


def test_fetch_client_preserves_malformed_gzip_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"not actually compressed"
    monkeypatch.setattr(
        "atlas.network.urlopen",
        lambda *_args, **_kwargs: _Response(
            body,
            headers={"Content-Encoding": "gzip"},
        ),
    )

    response = FetchClient().request("https://example.com/files/", body_limit=512)

    assert response.body == body
    assert response.body_truncated is False


def test_curl_fallback_requests_one_probe_byte_and_exposes_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_args(command: list[str], *, timeout: float) -> SubprocessResult:
        assert timeout > 0
        assert "--compressed" not in command
        assert command[command.index("--range") + 1] == "0-5"
        assert command[command.index("--max-filesize") + 1] == "6"
        header_path = Path(command[command.index("--dump-header") + 1])
        output_path = Path(command[command.index("--output") + 1])
        header_path.write_text(
            "HTTP/1.1 206 Partial Content\r\nContent-Type: text/plain\r\n\r\n",
            encoding="utf-8",
        )
        output_path.write_bytes(b"abcdef")
        return SubprocessResult(command, 0, "https://example.com/files/\n206", "")

    monkeypatch.setattr("atlas.network.run_args", fake_run_args)

    response = _fetch_with_curl(
        "/usr/bin/curl",
        "https://example.com/files/",
        FetchOptions(timeout=5),
        body_limit=5,
    )

    assert response is not None
    assert response.body == b"abcde"
    assert response.body_truncated is True


def test_curl_fallback_bounds_decompressed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = gzip.compress(b"a" * 1_000)

    def fake_run_args(command: list[str], *, timeout: float) -> SubprocessResult:
        assert timeout > 0
        assert "--compressed" not in command
        header_path = Path(command[command.index("--dump-header") + 1])
        output_path = Path(command[command.index("--output") + 1])
        header_path.write_text(
            "HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n",
            encoding="utf-8",
        )
        output_path.write_bytes(encoded)
        return SubprocessResult(command, 0, "https://example.com/files/\n200", "")

    monkeypatch.setattr("atlas.network.run_args", fake_run_args)

    response = _fetch_with_curl(
        "/usr/bin/curl",
        "https://example.com/files/",
        FetchOptions(timeout=5),
        body_limit=32,
    )

    assert response is not None
    assert response.body == b"a" * 32
    assert response.body_truncated is True


def test_curl_fallback_treats_max_filesize_stop_as_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_args(command: list[str], *, timeout: float) -> SubprocessResult:
        assert timeout > 0
        header_path = Path(command[command.index("--dump-header") + 1])
        output_path = Path(command[command.index("--output") + 1])
        header_path.write_text(
            "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n",
            encoding="utf-8",
        )
        output_path.write_bytes(b"abcde")
        return SubprocessResult(command, 63, "https://example.com/files/\n200", "too large")

    monkeypatch.setattr("atlas.network.run_args", fake_run_args)

    response = _fetch_with_curl(
        "/usr/bin/curl",
        "https://example.com/files/",
        FetchOptions(timeout=5),
        body_limit=5,
    )

    assert response is not None
    assert response.body == b"abcde"
    assert response.body_truncated is True
