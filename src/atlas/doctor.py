"""Runtime diagnostics."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from subprocess import TimeoutExpired
from types import ModuleType

from atlas import __version__
from atlas.config import AtlasSettings
from atlas.models import DoctorCheck, DoctorReport
from atlas.network import FetchClient, FetchError, FetchOptions
from atlas.paths import cache_dir, config_dir, data_dir, ensure_app_dirs, log_dir
from atlas.runner import run_args


@dataclass(frozen=True)
class Wget2Capabilities:
    """Feature flags reported by `wget2 --version`."""

    path: str
    version: str
    features: dict[str, bool]

    @property
    def detail(self) -> str:
        enabled = sorted(name for name, ok in self.features.items() if ok)
        suffix = f"; features: {', '.join(enabled)}" if enabled else ""
        return f"{self.path} ({self.version}){suffix}"


def _tool_version(executable: str) -> str | None:
    output = _tool_version_output(executable)
    if output is None:
        return None
    path, text = output
    first_line = text.splitlines()
    if not first_line:
        return path
    version = first_line[0].split(" Copyright", 1)[0]
    return f"{path} ({version})"


def _tool_version_output(executable: str) -> tuple[str, str] | None:
    path = which(executable)
    if not path:
        return None
    version_flag = "--version" if executable in {"aria2c", "wget2", "wget"} else "-version"
    try:
        result = run_args([path, version_flag], timeout=5)
    except (OSError, TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return path, result.stdout or result.stderr


def _wget2_capabilities() -> Wget2Capabilities | None:
    output = _tool_version_output("wget2")
    if output is None:
        return None
    path, text = output
    return _parse_wget2_capabilities(path, text)


def _parse_wget2_capabilities(path: str, text: str) -> Wget2Capabilities:
    lines = text.splitlines()
    version = (lines[0] if lines else "wget2").split(" Copyright", 1)[0]
    features: dict[str, bool] = {}
    for token in text.replace("(", " ").replace(")", " ").split():
        if len(token) < 2 or token[0] not in {"+", "-"}:
            continue
        name = token[1:].strip().lower()
        if not name:
            continue
        enabled = token[0] == "+"
        features[name] = enabled
        if name == "brotlidec":
            features["brotli"] = enabled
        if name.startswith("ssl/"):
            features["ssl"] = enabled
    return Wget2Capabilities(path=path, version=version, features=features)


def _load_ytdlp() -> ModuleType | None:
    try:
        return importlib.import_module("yt_dlp")
    except ImportError:
        return None


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _certifi_path() -> str | None:
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def _https_probe() -> tuple[bool, str, str | None]:
    try:
        response = FetchClient().get(
            "https://www.python.org/",
            FetchOptions(timeout=3.0, user_agent="atlas/doctor"),
            fallback_tools=False,
        )
    except FetchError as exc:
        return False, exc.failure.message, "Check Python certificates or run `atlas setup`."
    return True, f"verified HTTPS ({response.status_code})", None


def _ytdlp_version(module: ModuleType) -> str:
    version_module = getattr(module, "version", None)
    version = getattr(version_module, "__version__", None)
    if isinstance(version, str):
        return version
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _writable_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".atlas-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return False, str(exc)
    return True, str(path)


def _package_version() -> str:
    try:
        return importlib.metadata.version("atlas")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def run_doctor(settings: AtlasSettings) -> DoctorReport:
    ensure_app_dirs()
    checks: list[DoctorCheck] = []

    python_ok = sys.version_info >= (3, 12)
    checks.append(
        DoctorCheck(
            name="Python",
            ok=python_ok,
            detail=sys.version.split()[0],
            hint="Install Python 3.12 or newer." if not python_ok else None,
        )
    )
    checks.append(DoctorCheck(name="atlas package", ok=True, detail=_package_version()))
    checks.append(DoctorCheck(name="Python SSL", ok=True, detail=ssl.OPENSSL_VERSION))
    certifi_path = _certifi_path()
    checks.append(
        DoctorCheck(
            name="CA bundle",
            ok=certifi_path is not None,
            required=False,
            detail=certifi_path or "using platform default trust store",
            hint="Install certifi or use a backend fallback if Python cannot verify this host.",
        )
    )
    https_ok, https_detail, https_hint = _https_probe()
    checks.append(
        DoctorCheck(
            name="HTTPS verification",
            ok=https_ok,
            required=False,
            detail=https_detail,
            hint=https_hint,
        )
    )

    ytdlp = _load_ytdlp()
    checks.append(
        DoctorCheck(
            name="yt-dlp",
            ok=ytdlp is not None,
            detail=_ytdlp_version(ytdlp) if ytdlp else "not importable",
            hint="Reinstall atlas so its required yt-dlp dependency is present."
            if ytdlp is None
            else None,
        )
    )

    for tool in ("ffmpeg", "ffprobe"):
        version = _tool_version(tool)
        checks.append(
            DoctorCheck(
                name=tool,
                ok=version is not None,
                detail=version or "not found",
                hint=(
                    f"Install with `brew install ffmpeg` to provide {tool}."
                    if version is None
                    else None
                ),
            )
        )

    aria2_version = _tool_version("aria2c")
    checks.append(
        DoctorCheck(
            name="aria2c",
            ok=aria2_version is not None,
            required=False,
            detail=aria2_version or "not found",
            hint=(
                "Optional: install with `brew install aria2` for segmented files, "
                "Metalink manifests, and shared batch queues."
            ),
        )
    )
    checks.append(
        DoctorCheck(
            name="aria2 Metalink/RPC",
            ok=aria2_version is not None,
            required=False,
            detail=(
                "available via aria2c JSON-RPC"
                if aria2_version is not None
                else "aria2c not found"
            ),
            hint="Install `aria2c` to expand .meta4/.metalink files and queue file batches."
            if aria2_version is None
            else None,
        )
    )

    wget2_capabilities = _wget2_capabilities()
    checks.append(
        DoctorCheck(
            name="wget2",
            ok=wget2_capabilities is not None,
            required=False,
            detail=wget2_capabilities.detail if wget2_capabilities else "not found",
            hint="Optional: install with `brew install wget2` for website mirroring.",
        )
    )
    if wget2_capabilities is not None:
        for feature in ("http2", "psl", "brotli", "zstd", "gpgme", "hsts"):
            available = wget2_capabilities.features.get(feature, False)
            checks.append(
                DoctorCheck(
                    name=f"wget2 {feature}",
                    ok=available,
                    required=False,
                    detail="available" if available else "missing",
                )
            )

    wget_version = _tool_version("wget")
    checks.append(
        DoctorCheck(
            name="wget",
            ok=wget_version is not None,
            required=False,
            detail=wget_version or "not found",
            hint="Optional: install with `brew install wget` for website mirroring fallback.",
        )
    )

    for label, directory in (
        ("config dir", config_dir()),
        ("data dir", data_dir()),
        ("cache dir", cache_dir()),
        ("log dir", log_dir()),
        ("output dir", settings.output_dir),
    ):
        ok, detail = _writable_dir(directory)
        checks.append(
            DoctorCheck(
                name=label,
                ok=ok,
                detail=detail,
                hint=f"Make {directory} writable." if not ok else None,
            )
        )

    cookie_support = ytdlp is not None and hasattr(ytdlp, "cookies")
    checks.append(
        DoctorCheck(
            name="browser cookie support",
            ok=cookie_support,
            required=False,
            detail="yt-dlp cookies module available" if cookie_support else "not available",
        )
    )
    curl_cffi = _module_available("curl_cffi")
    checks.append(
        DoctorCheck(
            name="yt-dlp impersonation",
            ok=curl_cffi,
            required=False,
            detail="curl_cffi available" if curl_cffi else "curl_cffi not installed",
            hint="Install `curl_cffi` to use media --impersonate browser profiles."
            if not curl_cffi
            else None,
        )
    )
    return DoctorReport(checks=checks)
