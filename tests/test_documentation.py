from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import SplitResult, unquote, urlsplit

from typer.main import get_command

from atlas.cli import app
from atlas.config import AtlasSettings
from atlas.models import ProgressMode

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "docs").glob("*.md")),
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
IPV4_LITERAL = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOST_LIKE_TARGET = re.compile(
    r"(?<![@\w.-])(?:https?://)?"
    r"(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})"
    r"(?::\d{1,5})?(?:/[^\s`|)]*)?",
    flags=re.IGNORECASE,
)


def _link_parts(raw_target: str) -> SplitResult:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]

    return urlsplit(target)


def _local_link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    parsed = _link_parts(raw_target)

    if parsed.scheme or parsed.netloc or target.startswith(("#", "mailto:")):
        return None
    return unquote(parsed.path)


def _heading_anchors(document: Path) -> set[str]:
    anchors: set[str] = set()
    duplicates: dict[str, int] = {}
    text = document.read_text(encoding="utf-8")

    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, flags=re.MULTILINE):
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        heading = re.sub(r"<[^>]+>", "", heading)
        base = re.sub(r"[^\w\- ]", "", heading.lower())
        base = re.sub(r"\s+", "-", base.strip())
        occurrence = duplicates.get(base, 0)
        duplicates[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")

    return anchors


def test_local_documentation_links_resolve() -> None:
    missing: list[str] = []

    for document in MARKDOWN_FILES:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = _local_link_target(raw_target)
            if not target:
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")

    assert not missing, "Broken local documentation links:\n" + "\n".join(missing)


def test_documentation_code_fences_are_balanced() -> None:
    unbalanced = [
        str(document.relative_to(ROOT))
        for document in MARKDOWN_FILES
        if len(re.findall(r"^```", document.read_text(encoding="utf-8"), flags=re.MULTILINE)) % 2
    ]

    assert not unbalanced, f"Unbalanced Markdown code fences: {unbalanced}"


def test_first_download_example_is_executable_and_explicit() -> None:
    guides = (ROOT / "README.md", ROOT / "docs" / "quick-start.md")
    example_url = "https://raw.githubusercontent.com/xkam7ar/atlas-download-manager/main/LICENSE"

    for guide in guides:
        text = guide.read_text(encoding="utf-8")
        assert "https://example.com/archive.zip" not in text
        assert example_url in text
        assert "--output-dir ./atlas-demo" in text
        assert "--kind file --backend native --output-dir ./atlas-demo" in text

    quick_start = (ROOT / "docs" / "quick-start.md").read_text(encoding="utf-8")
    assert "test -f ./atlas-demo/LICENSE" in quick_start
    assert "atlas inspect-session OUTPUT" in quick_start


def test_public_docs_do_not_activate_mutable_remote_install_sources() -> None:
    forbidden = (
        re.compile(r"raw\.githubusercontent\.com/xkam7ar/atlas-download-manager/main/install\.sh"),
        re.compile(
            r"uv tool install(?: --force)? [\"']?"
            r"git\+https://github\.com/xkam7ar/atlas-download-manager\.git(?!@)"
        ),
    )
    violations: list[str] = []

    for document in MARKDOWN_FILES:
        text = document.read_text(encoding="utf-8")
        for pattern in forbidden:
            if match := pattern.search(text):
                violations.append(
                    f"{document.relative_to(ROOT)}: mutable remote source {match.group(0)!r}"
                )

    assert not violations, "Mutable remote install sources in public docs:\n" + "\n".join(
        violations
    )


def test_public_documentation_has_no_public_ipv4_targets() -> None:
    exposed: list[str] = []

    for document in MARKDOWN_FILES:
        for candidate in IPV4_LITERAL.findall(document.read_text(encoding="utf-8")):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_global:
                exposed.append(f"{document.relative_to(ROOT)}: {candidate}")

    assert not exposed, "Public IPv4 literals in documentation:\n" + "\n".join(exposed)


def test_open_directory_audit_does_not_publish_live_target_inventory() -> None:
    audit = ROOT / "docs" / "open-directory-audit-2026-07-15.md"
    text = audit.read_text(encoding="utf-8")
    live_hosts = sorted(
        {
            match.group("host").lower()
            for match in HOST_LIKE_TARGET.finditer(text)
            if not match.group("host")
            .lower()
            .endswith((".example", ".invalid", ".localhost", ".test"))
        }
    )

    assert "Raw target identities are intentionally omitted" in text
    assert not re.search(r"^\|\s*#\s*\|\s*Target\b", text, flags=re.IGNORECASE | re.MULTILINE)
    assert not live_hosts, f"Live hosts in public open-directory audit: {live_hosts}"


def test_local_documentation_fragments_resolve() -> None:
    missing: list[str] = []

    for document in MARKDOWN_FILES:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            parsed = _link_parts(raw_target)
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            target = (
                document if not parsed.path else (document.parent / unquote(parsed.path)).resolve()
            )
            if target.suffix != ".md" or not target.exists():
                continue
            fragment = unquote(parsed.fragment).lower()
            if fragment not in _heading_anchors(target):
                missing.append(f"{document.relative_to(ROOT)} -> {raw_target}")

    assert not missing, "Broken documentation fragments:\n" + "\n".join(missing)


def test_command_reference_names_every_top_level_command() -> None:
    command_reference = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    registered = get_command(app).commands
    missing = sorted(
        name
        for name in registered
        if not re.search(rf"\batlas\s+{re.escape(name)}\b", command_reference)
    )

    assert not missing, f"Top-level commands missing from docs/commands.md: {missing}"


def test_configuration_reference_names_every_settings_field() -> None:
    reference = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `[^`]+` \| `([^`]+)` \|", reference, flags=re.MULTILINE))
    missing = sorted(set(AtlasSettings.model_fields) - documented)

    assert not missing, f"Settings fields missing from docs/configuration.md: {missing}"


def test_development_guide_names_every_source_module() -> None:
    guide = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    layout_match = re.search(
        r"^## Project layout\s+```text\n(?P<layout>.*?)\n```",
        guide,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert layout_match is not None, "docs/development.md has no Project layout block"
    documented = set(
        re.findall(r"^\s{2}([^/\s]+\.py)$", layout_match.group("layout"), flags=re.MULTILINE)
    )
    source_modules = {module.name for module in (ROOT / "src" / "atlas").glob("*.py")}
    missing = sorted(source_modules - documented)

    assert not missing, f"Source modules missing from docs/development.md: {missing}"


def test_architecture_map_names_every_substantive_source_module() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([^`]+\.py)` \|", architecture, flags=re.MULTILINE))
    source_modules = {
        module.name
        for module in (ROOT / "src" / "atlas").glob("*.py")
        if not module.name.startswith("__")
    }
    missing = sorted(source_modules - documented)

    assert not missing, f"Source modules missing from docs/architecture.md: {missing}"


def test_media_help_contracts_are_documented() -> None:
    reference = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    commands = get_command(app).commands

    for command_name in ("info", "formats"):
        playlist_option = next(
            param for param in commands[command_name].params if param.name == "playlist"
        )
        assert "--playlist" in playlist_option.opts
        assert re.search(
            rf"atlas {command_name}[^\n]*--playlist",
            reference,
        ), f"docs/commands.md omits atlas {command_name} --playlist"

    playlist_type = next(param for param in commands["playlist"].params if param.name == "kind")
    for choice in playlist_type.type.choices:
        assert f"--type {choice}" in reference

    for mode in ProgressMode:
        assert f"| `{mode.value}` |" in reference


def test_canonical_quality_gate_is_consistent() -> None:
    commands = (
        "uv run pytest",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src",
        "sh -n install.sh",
        "uv build",
        "git diff --check",
    )
    guides = (
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "development.md",
        ROOT / "docs" / "system-contracts.md",
    )

    for guide in guides:
        text = guide.read_text(encoding="utf-8")
        missing = [command for command in commands if command not in text]
        assert not missing, f"{guide.relative_to(ROOT)} omits quality gates: {missing}"
