"""Typed directory scan result contracts."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.directory_index import DirectoryEntry, directory_index_from_work_item
from atlas.models import ScanErrorCode, ScanStatus, WorkItem


@dataclass(frozen=True)
class ScanError:
    code: ScanErrorCode
    message: str
    url: str
    recoverable: bool = True


@dataclass(frozen=True)
class DirectoryScanResult:
    seed_url: str
    final_url: str | None
    status: ScanStatus
    entries: tuple[DirectoryEntry, ...]
    files: tuple[DirectoryEntry, ...]
    folders: tuple[DirectoryEntry, ...]
    html_pages: tuple[DirectoryEntry, ...]
    skipped: tuple[DirectoryEntry, ...]
    errors: tuple[ScanError, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status in {ScanStatus.success, ScanStatus.partial}


def directory_scan_result_from_work_item(scan: WorkItem) -> DirectoryScanResult:
    index = directory_index_from_work_item(scan)
    errors = tuple(
        _scan_error_from_mapping(error, default_url=scan.url) for error in scan.scan_errors
    )
    return DirectoryScanResult(
        seed_url=scan.url,
        final_url=scan.final_url,
        status=scan.scan_status,
        entries=index.entries,
        files=tuple(entry for entry in index.files if entry.kind != "html"),
        folders=index.folders,
        html_pages=tuple(entry for entry in index.files if entry.kind == "html"),
        skipped=index.skipped,
        errors=errors,
        warnings=tuple(scan.scan_warnings),
    )


def _scan_error_from_mapping(error: dict[str, object], *, default_url: str) -> ScanError:
    raw_code = str(error.get("code") or ScanErrorCode.connection_failed.value)
    try:
        code = ScanErrorCode(raw_code)
    except ValueError:
        code = ScanErrorCode.connection_failed
    return ScanError(
        code=code,
        message=str(error.get("message") or raw_code),
        url=str(error.get("url") or default_url),
        recoverable=bool(error.get("recoverable", True)),
    )
