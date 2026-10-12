"""Shared visual tokens for the Plotly report and the matplotlib PNGs.

Categorical slots are used in a fixed order (never cycled); magnitude uses a single-hue
blue ramp; status colours are reserved for good/bad deltas and always paired with a label.
"""

from __future__ import annotations

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]
ORDINAL_BLUE = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]

SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
GOOD = "#006300"
CRITICAL = "#d03b3b"
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

DEVICE_COLORS = {"desktop": SERIES[0], "mobile": SERIES[1], "tablet": SERIES[2]}


def money(value: float, decimals: int = 0) -> str:
    """$1,234 / $1.2M style formatting (ASCII only)."""
    v = float(value)
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.2f}M"
    if abs(v) >= 1e4:
        return f"${v / 1e3:,.1f}K"
    return f"${v:,.{decimals}f}"


def pct(value: float, decimals: int = 1) -> str:
    """0.1234 -> '12.3%'."""
    return f"{float(value) * 100:.{decimals}f}%"


def num(value: float) -> str:
    """Thousands-separated integer."""
    return f"{float(value):,.0f}"
