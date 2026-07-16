from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from atlas.network import FetchClient, FetchOptions, FetchResponse, _fetch_with_curl
from atlas.runner import SubprocessResult


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = BytesIO(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "text/plain"}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return "https://example.com/files/"

    def read(self, size: int) -> bytes:
        return self._body.read(size)


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


def test_curl_fallback_requests_one_probe_byte_and_exposes_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_args(command: list[str], *, timeout: float) -> SubprocessResult:
        assert timeout > 0
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
