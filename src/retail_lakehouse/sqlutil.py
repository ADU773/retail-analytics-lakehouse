"""Small helpers for building SQL text safely."""

from __future__ import annotations

from pathlib import Path


def sql_str(value: str) -> str:
    """Single-quoted SQL string literal with embedded quotes escaped."""
    return "'" + value.replace("'", "''") + "'"


def sql_path(path: Path | str) -> str:
    """SQL string literal for an absolute, forward-slash path (works on Windows too)."""
    return sql_str(Path(path).resolve().as_posix())
