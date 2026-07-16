from __future__ import annotations

from pathlib import Path

import atlas.doctor as doctor_module
from atlas.config import AtlasSettings
from atlas.doctor import Wget2Capabilities, _parse_wget2_capabilities, run_doctor


def test_doctor_reports_required_checks(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")

    report = run_doctor(settings)

    names = {check.name for check in report.checks}
    assert "Python" in names
    assert "atlas package" in names
    assert "yt-dlp" in names
    assert "mutagen" in names
    assert "ffmpeg" in names
    assert "ffprobe" in names
    assert "aria2c" in names
    assert "output dir" in names


def test_doctor_plan_only_does_not_create_output_path(tmp_path: Path) -> None:
    output_dir = tmp_path / "missing" / "output"
    settings = AtlasSettings(
        output_dir=output_dir,
        archive_file=tmp_path / "archive.txt",
    )

    report = run_doctor(settings, create_paths=False)

    output_check = next(check for check in report.checks if check.name == "output dir")
    assert output_check.ok is True
    assert not output_dir.exists()


def test_doctor_fails_when_required_tools_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")

    monkeypatch.setattr("atlas.doctor._tool_version", lambda _tool: None)

    report = run_doctor(settings)

    assert report.ok is False
    failed_required = {check.name for check in report.checks if check.required and not check.ok}
    assert {"ffmpeg", "ffprobe"}.issubset(failed_required)


def test_doctor_allows_missing_optional_aria2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")

    def fake_tool_version(tool: str) -> str | None:
        if tool == "aria2c":
            return None
        return f"{tool} ok"

    monkeypatch.setattr("atlas.doctor._tool_version", fake_tool_version)

    report = run_doctor(settings)

    aria2 = next(check for check in report.checks if check.name == "aria2c")
    assert aria2.ok is False
    assert aria2.required is False
    failed_required = {check.name for check in report.checks if check.required and not check.ok}
    assert "aria2c" not in failed_required


def test_doctor_reports_aria2_rpc_and_impersonation_support(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    monkeypatch.setattr("atlas.doctor._tool_version", lambda tool: f"{tool} ok")
    monkeypatch.setattr("atlas.doctor._module_available", lambda name: name == "curl_cffi")

    report = run_doctor(settings)

    checks = {check.name: check for check in report.checks}
    assert checks["aria2 Metalink/RPC"].ok is True
    assert checks["yt-dlp impersonation"].ok is True
    assert checks["yt-dlp impersonation"].detail == "curl_cffi available"


def test_doctor_reports_missing_ytdlp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")

    monkeypatch.setattr("atlas.doctor._tool_version", lambda tool: f"{tool} ok")
    monkeypatch.setattr("atlas.doctor._load_ytdlp", lambda: None)

    report = run_doctor(settings)

    ytdlp = next(check for check in report.checks if check.name == "yt-dlp")
    assert ytdlp.ok is False
    assert ytdlp.required is True
    assert report.ok is False


def test_doctor_reports_missing_required_mutagen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    real_module_available = doctor_module._module_available
    monkeypatch.setattr(
        "atlas.doctor._module_available",
        lambda name: False if name == "mutagen" else real_module_available(name),
    )

    report = run_doctor(settings)

    mutagen = next(check for check in report.checks if check.name == "mutagen")
    assert mutagen.ok is False
    assert mutagen.required is True
    assert report.ok is False


def test_parse_wget2_capabilities_extracts_feature_flags() -> None:
    text = "\n".join(
        [
            "GNU Wget2 2.2.1 - multithreaded metalink/file/website downloader",
            "+digest +https +ssl/gnutls +ipv6 +iri +large-file +nls -ntlm +psl -hsts",
            "+iconv +idn2 +zlib +brotlidec +zstd +bzip2 +lzip +http2 +gpgme",
        ]
    )

    capabilities = _parse_wget2_capabilities("/opt/bin/wget2", text)

    assert (
        capabilities.version == "GNU Wget2 2.2.1 - multithreaded metalink/file/website downloader"
    )
    assert capabilities.features["http2"] is True
    assert capabilities.features["psl"] is True
    assert capabilities.features["brotli"] is True
    assert capabilities.features["zstd"] is True
    assert capabilities.features["gpgme"] is True
    assert capabilities.features["hsts"] is False


def test_doctor_reports_wget2_feature_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    monkeypatch.setattr("atlas.doctor._tool_version", lambda tool: f"{tool} ok")
    monkeypatch.setattr(
        "atlas.doctor._wget2_capabilities",
        lambda: Wget2Capabilities(
            path="/opt/bin/wget2",
            version="GNU Wget2 2.2.1",
            features={
                "http2": True,
                "psl": True,
                "brotli": False,
                "zstd": True,
                "gpgme": False,
                "hsts": True,
            },
        ),
    )

    report = run_doctor(settings)

    checks = {check.name: check for check in report.checks}
    assert checks["wget2 http2"].ok is True
    assert checks["wget2 brotli"].ok is False
    assert checks["wget2 brotli"].required is False
    assert checks["wget2 gpgme"].detail == "missing"
