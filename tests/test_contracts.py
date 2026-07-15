from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from atlas.cli import _write_batch_artifacts
from atlas.models import (
    AdaptiveDownloadPlan,
    AdaptivePoliteness,
    BatchEntry,
    BatchItemResult,
    BatchKind,
    BatchSummary,
    DownloadStatus,
    EngineKind,
    FileSizeClass,
    HubKind,
    ProgressEvent,
    ProgressPhase,
    SmartDownloadSession,
    WorkBucket,
    WorkItem,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_stable_json_model_field_contracts() -> None:
    assert tuple(WorkItem.model_fields) == (
        "url",
        "host",
        "final_url",
        "final_host",
        "redirect_target",
        "kind",
        "content_type",
        "content_length",
        "content_disposition",
        "content_disposition_filename",
        "filename",
        "file_extension",
        "accept_ranges",
        "supports_ranges",
        "etag",
        "last_modified",
        "discovered_links",
        "discovered_work_items",
        "sitemap_urls",
        "robots_url",
        "url_fingerprint",
        "mirror_fingerprint",
        "classification_notes",
        "warning_flags",
        "same_host",
        "external_host",
        "scan_type",
        "scan_recommended_mode",
        "scan_recommended_strategy",
        "scan_counts",
        "scan_estimated_bytes",
        "scan_warnings",
        "scan_status",
        "scan_errors",
        "size_class",
        "bucket",
        "selected_backend",
        "priority",
        "recursion_depth",
        "checksum_metadata",
        "scheduler_decision",
        "probed",
        "error",
    )
    assert tuple(AdaptiveDownloadPlan.model_fields) == (
        "enabled",
        "politeness",
        "global_min_concurrency",
        "global_max_concurrency",
        "queue_concurrency",
        "per_host_concurrency",
        "per_file_segments",
        "per_file_segment_cap",
        "max_active_files",
        "max_total_connections",
        "max_per_host_connections",
        "max_active_postprocessors",
        "max_disk_write_bytes_per_sec",
        "speed_limit",
        "backend",
        "strategy",
        "size_counts",
        "bucket_counts",
        "hosts",
        "work_items",
        "safety_notes",
    )
    assert tuple(SmartDownloadSession.model_fields) == (
        "source",
        "detected_kind",
        "intent",
        "session_type",
        "manifest",
        "plan",
        "customization",
        "scheduler_policy",
        "progress_reporter",
        "final_summary",
    )
    assert tuple(ProgressEvent.model_fields) == (
        "engine",
        "status",
        "phase",
        "kind",
        "filename",
        "title",
        "url",
        "item_id",
        "line_no",
        "downloaded_bytes",
        "total_bytes",
        "estimated_bytes",
        "fragment_index",
        "fragment_count",
        "files_done",
        "files_total",
        "percent",
        "retry_count",
        "active_connections",
        "queue_concurrency",
        "per_host_concurrency",
        "per_file_segments",
        "max_total_connections",
        "max_per_host_connections",
        "max_active_postprocessors",
        "priority",
        "recursion_depth",
        "size_class",
        "work_bucket",
        "selected_backend",
        "scheduler_decision",
        "speed_limit",
        "reclassified_from",
        "speed_bytes_per_sec",
        "eta_seconds",
        "backend_id",
        "error_code",
        "verified_bytes",
        "verification_pending",
        "piece_length",
        "piece_count",
        "bitfield",
        "followed_by",
        "following",
        "belongs_to",
        "backend_files",
        "message",
    )
    assert tuple(BatchSummary.model_fields) == (
        "kind",
        "total",
        "succeeded",
        "failed",
        "skipped",
        "canceled",
        "results",
    )


def test_stable_json_sample_payloads_round_trip_with_expected_keys() -> None:
    item = WorkItem(
        url="https://example.com/archive.zip",
        host="example.com",
        final_url="https://cdn.example.com/archive.zip",
        final_host="cdn.example.com",
        redirect_target="https://cdn.example.com/archive.zip",
        kind=HubKind.file,
        content_type="application/zip",
        content_length=123456,
        filename="archive.zip",
        file_extension=".zip",
        accept_ranges="bytes",
        supports_ranges=True,
        url_fingerprint="url-fp",
        mirror_fingerprint="mirror-fp",
        classification_notes=["direct file"],
        warning_flags=["redirected"],
        same_host=False,
        external_host=True,
        size_class=FileSizeClass.medium,
        bucket=WorkBucket.medium,
        selected_backend="aria2",
        priority=40,
        recursion_depth=0,
        checksum_metadata={"sha256": "ab12"},
        scheduler_decision="medium: moderate queue with segmented transfer",
    )
    plan = AdaptiveDownloadPlan(
        enabled=True,
        politeness=AdaptivePoliteness.normal,
        global_min_concurrency=2,
        global_max_concurrency=16,
        queue_concurrency=8,
        per_host_concurrency=4,
        per_file_segments=4,
        per_file_segment_cap=16,
        max_active_files=8,
        max_total_connections=32,
        max_per_host_connections=8,
        max_active_postprocessors=2,
        max_disk_write_bytes_per_sec=200_000_000,
        speed_limit="50M",
        backend="mixed",
        strategy="adaptive mixed manifest",
        size_counts={"medium": 1},
        bucket_counts={"medium": 1},
        hosts={"example.com": 1},
        work_items=[item],
        safety_notes=["same-host cap enforced"],
    )
    session = SmartDownloadSession(
        source="batch.txt",
        detected_kind=HubKind.auto,
        intent="batch_auto",
        session_type="batch_session",
        manifest=[item],
        plan=plan,
        customization={"output_dir": "/tmp/atlas", "kind": "auto"},
        scheduler_policy={"mode": "adaptive", "queue_concurrency": 8},
        progress_reporter="batch_rich",
        final_summary={"total": 1, "failed": 0},
    )
    event = ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        phase=ProgressPhase.download,
        kind=HubKind.file,
        filename="archive.zip",
        url=item.url,
        item_id="1",
        line_no=1,
        downloaded_bytes=42,
        total_bytes=123456,
        percent=12.5,
        retry_count=1,
        active_connections=4,
        queue_concurrency=8,
        per_host_concurrency=4,
        per_file_segments=4,
        max_total_connections=32,
        max_per_host_connections=8,
        max_active_postprocessors=2,
        priority=40,
        recursion_depth=0,
        size_class=FileSizeClass.medium,
        work_bucket=WorkBucket.medium,
        selected_backend="aria2",
        scheduler_decision="lane stable",
        speed_limit="50M",
        speed_bytes_per_sec=1024.0,
        eta_seconds=30.0,
        backend_id="gid",
        error_code=None,
        verified_bytes=0,
        verification_pending=True,
        piece_length=262144,
        piece_count=4,
        bitfield="f",
        followed_by=["https://example.com/next"],
        following=None,
        belongs_to=None,
        backend_files=[{"path": "archive.zip"}],
        message="downloading",
    )
    summary = BatchSummary(
        kind=BatchKind.file,
        total=1,
        succeeded=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url=item.url),
                status=DownloadStatus.success,
                message="Saved",
                plan={"kind": "file", "engine": "aria2"},
            )
        ],
    )

    for model in (item, plan, session, event, summary):
        payload = _json_payload(model)
        assert set(payload) == set(model.__class__.model_fields)
        assert model.__class__.model_validate(payload) == model

    event_payload = event.model_dump(mode="json", exclude_none=True)
    assert event_payload["engine"] == "aria2c"
    assert event_payload["phase"] == "download"
    assert event_payload["kind"] == "file"
    assert event_payload["size_class"] == "medium"
    assert event_payload["work_bucket"] == "medium"
    assert event_payload["selected_backend"] == "aria2"
    assert event_payload["backend_files"] == [{"path": "archive.zip"}]

    session_payload = session.model_dump(mode="json")
    assert session_payload["plan"]["work_items"][0]["selected_backend"] == "aria2"
    assert session_payload["scheduler_policy"] == {"mode": "adaptive", "queue_concurrency": 8}


def test_latest_batch_artifact_shape_is_stable(tmp_path: Path) -> None:
    summary = BatchSummary(
        kind=BatchKind.file,
        total=4,
        succeeded=1,
        failed=1,
        skipped=1,
        canceled=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url="https://example.com/ok.bin"),
                status=DownloadStatus.success,
                message="Saved",
                plan={"kind": "file", "engine": "native"},
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=2, url="https://example.com/bad.bin"),
                status=DownloadStatus.failed,
                message="checksum mismatch",
                plan={
                    "kind": "file",
                    "engine": "aria2",
                    "args": [
                        "/usr/bin/aria2c",
                        "--header=Authorization: <redacted>",
                        "https://example.com/bad.bin",
                    ],
                },
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=3, url="https://example.com/unknown"),
                status=DownloadStatus.skipped,
                message="unknown route",
                plan=None,
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=4, url="https://example.com/cancel.bin"),
                status=DownloadStatus.canceled,
                message="canceled by operator",
                plan={"kind": "file", "engine": "native"},
            ),
        ],
    )
    adaptive_plan = AdaptiveDownloadPlan(
        enabled=True,
        queue_concurrency=4,
        per_host_concurrency=2,
        per_file_segments=2,
        per_file_segment_cap=8,
        max_active_files=4,
        max_total_connections=8,
        max_per_host_connections=4,
        backend="mixed",
        strategy="adaptive test",
        work_items=[
            WorkItem(
                url="https://example.com/bad.bin",
                host="example.com",
                kind=HubKind.file,
                bucket=WorkBucket.small,
                selected_backend="aria2",
            )
        ],
    )

    paths = _write_batch_artifacts(
        summary,
        output_dir=tmp_path,
        adaptive_plan=adaptive_plan,
        source="atlas-test-urls.txt",
    )

    latest = tmp_path / ".atlas" / "latest"
    assert paths.keys() >= {
        "summary",
        "manifest",
        "latest_summary",
        "latest_manifest",
        "failed",
        "skipped",
        "canceled",
        "retry_manifest",
        "retry",
    }
    assert {path.name for path in latest.iterdir()} == {
        "summary.json",
        "manifest.json",
        "failed.txt",
        "skipped.txt",
        "canceled.txt",
        "retry.atlas.json",
    }

    latest_summary = _read_json(latest / "summary.json")
    latest_manifest = _read_json(latest / "manifest.json")
    retry_manifest = _read_json(latest / "retry.atlas.json")

    assert set(latest_summary) == set(BatchSummary.model_fields)
    assert BatchSummary.model_validate(latest_summary).failed == 1
    assert set(latest_manifest) == {
        "created_at",
        "kind",
        "total",
        "succeeded",
        "failed",
        "skipped",
        "canceled",
        "smart_session",
        "adaptive_plan",
        "items",
        "artifacts",
    }
    assert set(latest_manifest["artifacts"]) == {
        "summary",
        "manifest",
        "failed",
        "skipped",
        "canceled",
        "retry",
    }
    assert set(latest_manifest["items"][0]) == {
        "line_no",
        "url",
        "status",
        "kind",
        "engine",
        "message",
        "backend_args",
        "backend_command",
    }
    assert latest_manifest["items"][1]["backend_args"] == [
        "/usr/bin/aria2c",
        "--header=Authorization: <redacted>",
        "https://example.com/bad.bin",
    ]
    assert "Authorization: <redacted>" in latest_manifest["items"][1]["backend_command"]
    assert latest_manifest["smart_session"]["source"] == "atlas-test-urls.txt"
    assert latest_manifest["smart_session"]["session_type"] == "batch_session"
    assert latest_manifest["smart_session"]["plan"]["work_items"][0]["selected_backend"] == "aria2"
    assert latest_manifest["adaptive_plan"]["queue_concurrency"] == 4

    assert set(retry_manifest) == {
        "version",
        "created_at",
        "kind",
        "manifest_path",
        "summary_path",
        "retry_failed_only",
        "retry_checksum_failures_only",
        "retry_skipped_unknowns_only",
        "retry_canceled_only",
        "export_failed_urls",
        "save_manifest",
        "load_manifest",
        "resume_previous_session",
        "skipped_urls",
        "canceled_urls",
    }
    assert retry_manifest["retry_failed_only"] == ["https://example.com/bad.bin"]
    assert retry_manifest["retry_checksum_failures_only"] == ["https://example.com/bad.bin"]
    assert retry_manifest["retry_skipped_unknowns_only"] == ["https://example.com/unknown"]
    assert retry_manifest["retry_canceled_only"] == ["https://example.com/cancel.bin"]
    assert (latest / "failed.txt").read_text(encoding="utf-8") == "https://example.com/bad.bin\n"
    assert (latest / "skipped.txt").read_text(encoding="utf-8") == "https://example.com/unknown\n"
    assert (latest / "canceled.txt").read_text(encoding="utf-8") == (
        "https://example.com/cancel.bin\n"
    )


def test_backend_layers_do_not_import_or_render_ui() -> None:
    backend_modules = (
        "src/atlas/adapters.py",
        "src/atlas/aria2_rpc.py",
        "src/atlas/backends.py",
        "src/atlas/engine.py",
        "src/atlas/file_probe.py",
        "src/atlas/progress_events.py",
        "src/atlas/runner.py",
    )
    forbidden_imports = ("rich", "atlas.progress", "atlas.theme", "atlas.views")
    forbidden_calls = {"print", "Console", "Group", "Live", "Panel", "Progress", "Table", "Text"}

    for relative_path in backend_modules:
        tree = _module_ast(relative_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _module_is_forbidden(alias.name, forbidden_imports), (
                        relative_path,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not _module_is_forbidden(module, forbidden_imports), (relative_path, module)
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                assert name not in forbidden_calls, (relative_path, name)
                assert not name.endswith(".print"), (relative_path, name)


def test_cli_and_menu_keep_raw_backend_construction_in_advanced_paths() -> None:
    cli_tree = _module_ast("src/atlas/cli.py")
    menu_tree = _module_ast("src/atlas/menu.py")

    assert _imported_names(cli_tree, "yt_dlp") == set()
    assert _imported_names(menu_tree, "yt_dlp") == set()
    assert "plan_backend_command" in _called_functions_in_scope(
        cli_tree,
        "_run_backend_passthrough",
    )
    assert "run_backend_command" in _called_functions_in_scope(cli_tree, "_run_backend_passthrough")
    assert _called_functions_outside_scopes(
        cli_tree,
        {"plan_backend_command", "run_backend_command"},
        allowed_scopes={"_run_backend_passthrough"},
    ) == {}
    assert _called_functions_outside_scopes(
        menu_tree,
        {"split"},
        allowed_scopes={"_advanced_backend_flow", "_parse_pasted_urls"},
        owner="shlex",
    ) == {}

    forbidden_preset_builders = {
        "build_audio_opts",
        "build_video_opts",
        "build_info_opts",
        "redact_ydl_opts",
    }
    assert _imported_names(cli_tree, "atlas.presets").isdisjoint(forbidden_preset_builders)
    assert _imported_names(menu_tree, "atlas.presets").isdisjoint(forbidden_preset_builders)
    assert _called_functions_outside_scopes(
        cli_tree,
        forbidden_preset_builders,
        allowed_scopes=set(),
    ) == {}
    assert _called_functions_outside_scopes(
        menu_tree,
        forbidden_preset_builders,
        allowed_scopes=set(),
    ) == {}


def _json_payload(model: BaseModel) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    assert isinstance(payload, dict)
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _module_ast(relative_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def _module_is_forbidden(module: str, forbidden: tuple[str, ...]) -> bool:
    return any(module == value or module.startswith(f"{value}.") for value in forbidden)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = _attribute_owner(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _attribute_owner(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _attribute_owner(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _imported_names(tree: ast.Module, module_name: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(f"{module_name}."):
                    names.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom) and node.module == module_name:
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _called_functions_in_scope(tree: ast.Module, scope_name: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == scope_name:
            return {_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
    raise AssertionError(f"Scope not found: {scope_name}")


def _called_functions_outside_scopes(
    tree: ast.Module,
    names: set[str],
    *,
    allowed_scopes: set[str],
    owner: str | None = None,
) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for scope_name, call in _scoped_calls(tree):
        call_name = _call_name(call)
        if owner is not None:
            if call_name != f"{owner}.{next(iter(names))}":
                continue
            name = next(iter(names))
        else:
            name = call_name.rsplit(".", 1)[-1]
        if name in names and scope_name not in allowed_scopes:
            violations.setdefault(scope_name, []).append(call_name)
    return violations


def _scoped_calls(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    scoped: list[tuple[str, ast.Call]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope_stack = ["<module>"]

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            scoped.append((self.scope_stack[-1], node))
            self.generic_visit(node)

    Visitor().visit(tree)
    return scoped
