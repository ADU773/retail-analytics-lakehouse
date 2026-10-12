"""Logging configuration (ASCII-only console output, safe for Windows cp1252 pipes)."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


class _AsciiFormatter(logging.Formatter):
    """Formatter that replaces any non-ASCII character so pipes never crash."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.encode("ascii", "replace").decode("ascii")


def configure_logging(level: str | int = "INFO") -> None:
    """Configure the ``retail_lakehouse`` logger hierarchy once."""
    root = logging.getLogger("retail_lakehouse")
    root.setLevel(level if isinstance(level, int) else level.upper())
    if not any(getattr(h, "_rlh", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_AsciiFormatter(LOG_FORMAT, datefmt="%H:%M:%S"))
        handler._rlh = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of ``retail_lakehouse``."""
    if not name.startswith("retail_lakehouse"):
        name = f"retail_lakehouse.{name}"
    return logging.getLogger(name)
