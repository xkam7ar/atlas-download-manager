"""Shared HTTP fetch helpers for scan/probe code."""

from __future__ import annotations

import ssl
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from shutil import which
from subprocess import TimeoutExpired
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from atlas.models import ScanErrorCode
from atlas.runner import run_args


class FetchErrorCode(StrEnum):
    tls_cert_verify_failed = "tls_cert_verify_failed"
    timeout = "timeout"
    connection_failed = "connection_failed"
    http_error = "http_error"


@dataclass(frozen=True)
class FetchOptions:
    timeout: float = 30.0
    user_agent: str = "atlas/0.1"
    verify_tls: bool = True
    ca_bundle: Path | None = None
    proxy: str | None = None
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    warnings: tuple[str, ...] = ()
    body_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _normalize_response_headers(self.headers))


@dataclass(frozen=True)
class FetchFailure:
    code: FetchErrorCode
    message: str
    url: str
    recoverable: bool = True
    status_code: int | None = None


class FetchError(RuntimeError):
    """Raised when Atlas cannot fetch a URL for lightweight scanning."""

    def __init__(self, failure: FetchFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class FetchClient:
    """Small verified HTTP client with safe backend fallback for scanner fetches."""

    def get(
        self,
        url: str,
        options: FetchOptions | None = None,
        *,
        fallback_tools: bool = False,
    ) -> FetchResponse:
        return self.request(url, options, method="GET", fallback_tools=fallback_tools)

    def head(
        self,
        url: str,
        options: FetchOptions | None = None,
    ) -> FetchResponse:
        return self.request(url, options, method="HEAD", body_limit=0)

    def request(
        self,
        url: str,
        options: FetchOptions | None = None,
        *,
        method: str = "GET",
        fallback_tools: bool = False,
        extra_headers: dict[str, str] | None = None,
        body_limit: int = 512 * 1024,
    ) -> FetchResponse:
        opts = options or FetchOptions()
        try:
            return self._request_urllib(
                url,
                opts,
                method=method,
                extra_headers=extra_headers,
                body_limit=body_limit,
            )
        except FetchError as exc:
            if (
                fallback_tools
                and method.upper() == "GET"
                and exc.failure.code == FetchErrorCode.tls_cert_verify_failed
            ):
                fallback = self._get_with_tool(url, opts, body_limit=body_limit)
                if fallback is not None:
                    return FetchResponse(
                        url=fallback.url,
                        final_url=fallback.final_url,
                        status_code=fallback.status_code,
                        headers=fallback.headers,
                        body=fallback.body,
                        warnings=("Python TLS verification failed; scanned using curl fallback.",),
                        body_truncated=fallback.body_truncated,
                    )
            raise

    def _request_urllib(
        self,
        url: str,
        options: FetchOptions,
        *,
        method: str,
        extra_headers: dict[str, str] | None,
        body_limit: int,
    ) -> FetchResponse:
        headers = _request_headers(options)
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, headers=headers, method=method.upper())
        try:
            with urlopen(
                request,
                timeout=options.timeout,
                context=_ssl_context(options),
            ) as response:
                raw_body = response.read(body_limit + 1) if body_limit > 0 else b""
                return FetchResponse(
                    url=url,
                    final_url=response.geturl(),
                    status_code=getattr(response, "status", 200),
                    headers=dict(response.headers.items()),
                    body=raw_body[:body_limit] if body_limit > 0 else b"",
                    body_truncated=body_limit > 0 and len(raw_body) > body_limit,
                )
        except HTTPError as exc:
            raise FetchError(
                FetchFailure(
                    code=FetchErrorCode.http_error,
                    message=f"HTTP {exc.code}: {exc.reason}",
                    url=url,
                    recoverable=exc.code >= 500 or exc.code in {408, 429},
                    status_code=exc.code,
                )
            ) from exc
        except TimeoutError as exc:
            raise FetchError(
                FetchFailure(
                    code=FetchErrorCode.timeout,
                    message=str(exc) or "timed out",
                    url=url,
                )
            ) from exc
        except URLError as exc:
            raise FetchError(_failure_from_url_error(url, exc)) from exc
        except OSError as exc:
            raise FetchError(
                FetchFailure(
                    code=FetchErrorCode.connection_failed,
                    message=str(exc),
                    url=url,
                )
            ) from exc

    def _get_with_tool(
        self,
        url: str,
        options: FetchOptions,
        *,
        body_limit: int,
    ) -> FetchResponse | None:
        curl = which("curl")
        if curl is None:
            return None
        return _fetch_with_curl(curl, url, options, body_limit=body_limit)


def scan_error_code_from_fetch(code: FetchErrorCode) -> ScanErrorCode:
    if code == FetchErrorCode.tls_cert_verify_failed:
        return ScanErrorCode.tls_failed
    if code == FetchErrorCode.timeout:
        return ScanErrorCode.timeout
    if code == FetchErrorCode.http_error:
        return ScanErrorCode.http_error
    return ScanErrorCode.connection_failed


def _ssl_context(options: FetchOptions) -> ssl.SSLContext | None:
    if not options.verify_tls:
        return ssl._create_unverified_context()
    cafile = str(options.ca_bundle) if options.ca_bundle else _certifi_bundle()
    return ssl.create_default_context(cafile=cafile)


def _certifi_bundle() -> str | None:
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def _request_headers(options: FetchOptions) -> dict[str, str]:
    headers = {"User-Agent": options.user_agent}
    for raw in options.headers:
        key, separator, value = raw.partition(":")
        if separator:
            headers[key.strip()] = value.strip()
    return headers


def _normalize_response_headers(headers: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, value in headers.items():
        name = raw_name.strip()
        if not name:
            continue
        canonical = "-".join(part.capitalize() for part in name.split("-"))
        if name.casefold() == "etag":
            canonical = "ETag"
        normalized[canonical] = value
    return normalized


def _failure_from_url_error(url: str, exc: URLError) -> FetchFailure:
    if _is_tls_certificate_error(exc):
        return FetchFailure(
            code=FetchErrorCode.tls_cert_verify_failed,
            message="TLS certificate verification failed",
            url=url,
            recoverable=True,
        )
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return FetchFailure(code=FetchErrorCode.timeout, message=str(reason), url=url)
    return FetchFailure(
        code=FetchErrorCode.connection_failed,
        message=str(reason or exc),
        url=url,
    )


def _is_tls_certificate_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return (
        isinstance(reason, ssl.SSLCertVerificationError)
        or (isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason))
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _fetch_with_curl(
    curl: str,
    url: str,
    options: FetchOptions,
    *,
    body_limit: int,
) -> FetchResponse | None:
    with tempfile.TemporaryDirectory(prefix="atlas-fetch-") as tmp:
        tmp_path = Path(tmp)
        header_path = tmp_path / "headers.txt"
        body_path = tmp_path / "body.bin"
        command = [
            curl,
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--max-time",
            str(max(1, int(options.timeout))),
            "--user-agent",
            options.user_agent,
            "--dump-header",
            str(header_path),
            "--output",
            str(body_path),
            "--write-out",
            "%{url_effective}\n%{http_code}",
        ]
        for raw in options.headers:
            command.extend(["--header", raw])
        if body_limit > 0:
            command.extend(
                [
                    "--range",
                    f"0-{body_limit}",
                    "--max-filesize",
                    str(body_limit + 1),
                ]
            )
        if options.proxy:
            command.extend(["--proxy", options.proxy])
        command.append(url)
        try:
            result = run_args(command, timeout=options.timeout + 5)
        except (OSError, TimeoutExpired):
            return None
        # curl exits 63 when --max-filesize stops a server that ignored Range.
        # Preserve a bounded partial response so callers can reject it as truncated.
        if result.returncode not in {0, 63}:
            return None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        final_url = lines[-2] if len(lines) >= 2 else url
        try:
            status_code = int(lines[-1]) if lines else 200
        except ValueError:
            status_code = 200
        if body_path.exists() and body_limit > 0:
            with body_path.open("rb") as body_file:
                raw_body = body_file.read(body_limit + 1)
        else:
            raw_body = b""
        response_headers = (
            _parse_curl_headers(header_path.read_text(encoding="utf-8", errors="replace"))
            if header_path.exists()
            else {}
        )
        return FetchResponse(
            url=url,
            final_url=final_url,
            status_code=status_code,
            headers=response_headers,
            body=raw_body[:body_limit] if body_limit > 0 else b"",
            body_truncated=(
                body_limit > 0 and (result.returncode == 63 or len(raw_body) > body_limit)
            ),
        )


def _parse_curl_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    blocks = [block for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    for line in (blocks[-1] if blocks else "").splitlines()[1:]:
        key, separator, value = line.partition(":")
        if separator:
            headers[key.strip()] = value.strip()
    return headers
