from __future__ import annotations

import logging
from pathlib import Path

import pytest

import atlas.logging as atlas_logging
from atlas.logging import configure_logging


def test_verbose_debug_logs_go_to_file_not_terminal_stream(
    tmp_path: Path,
    capsys,
) -> None:
    log_file = tmp_path / "atlas.log"

    configure_logging(verbose=True, log_file=log_file)
    logger = logging.getLogger("atlas.test")
    logger.debug("debug only in file")
    logger.info("info can reach terminal")
    for handler in logging.getLogger().handlers:
        handler.flush()

    captured = capsys.readouterr()
    assert "debug only in file" not in captured.err
    assert "info can reach terminal" in captured.err
    log_text = log_file.read_text(encoding="utf-8")
    assert "debug only in file" in log_text
    assert "info can reach terminal" in log_text


def test_logs_are_private_rotating_and_redact_signed_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_file = tmp_path / "atlas.log"
    monkeypatch.setattr(atlas_logging, "_MAX_LOG_BYTES", 256)

    configure_logging(verbose=True, log_file=log_file)
    logger = logging.getLogger("atlas.secret-test")
    for index in range(20):
        logger.info(
            "download %s https://cdn.example/file?X-Goog-Signature=TOPSECRET&part=%s",
            index,
            index,
        )
    for handler in logging.getLogger().handlers:
        handler.flush()

    generations = [log_file, log_file.with_name("atlas.log.1")]
    assert all(path.exists() for path in generations)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in generations)
    combined = "".join(path.read_text(encoding="utf-8") for path in generations)
    assert "TOPSECRET" not in combined
    assert "X-Goog-Signature=<redacted>" in combined
