from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import SplitResult, unquote, urlsplit

from typer.main import get_command

from atlas.cli import app

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


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


def test_local_documentation_fragments_resolve() -> None:
    missing: list[str] = []

    for document in MARKDOWN_FILES:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            parsed = _link_parts(raw_target)
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            target = (
                document
                if not parsed.path
                else (document.parent / unquote(parsed.path)).resolve()
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
