"""Custom exceptions for atlas."""

from __future__ import annotations


class AtlasError(Exception):
    """Base application error."""


class ConfigError(AtlasError):
    """Raised when configuration cannot be loaded or validated."""


class DependencyMissingError(AtlasError):
    """Raised when a required runtime dependency is unavailable."""


class EngineError(AtlasError):
    """Raised when yt-dlp fails or returns an unexpected result."""


class BatchError(AtlasError):
    """Raised when batch input cannot be processed."""


class PlanningError(AtlasError):
    """Raised when user intent cannot be converted into safe yt-dlp options."""
