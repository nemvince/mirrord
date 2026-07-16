"""Server-side SVG chart rendering.

Thin wrappers around the ``charted`` library (pure-Python, zero runtime
dependencies, no JavaScript). Every function returns a standalone, fully
static ``<svg>`` string suitable for embedding directly into an HTML page.
"""

import re
from typing import Any

from charted import BarChart, ColumnChart, Theme

# Matches the fixed width/height attributes charted emits on the <svg> root.
_ROOT_RE = re.compile(
    r'(<svg\b[^>]*?)\s+width="[\d.]+"\s+height="[\d.]+"',
    re.IGNORECASE,
)


def _render(chart: Any) -> str:
    """Render a charted chart to a standalone, JS-free SVG string.

    charted emits fixed pixel ``width``/``height`` on the root ``<svg>``, which
    overflows narrow container boxes. We drop those attributes and add
    responsive styling so the chart scales to its parent while preserving its
    aspect ratio via the ``viewBox``.
    """
    svg = chart.to_svg()
    return _ROOT_RE.sub(
        r'\1 style="display:block;width:100%;height:auto"',
        svg,
        count=1,
    )


# Ayu-dark palette (kept for backwards-compatible imports / call sites).
INK = "#0a0e14"
DIVIDE = "#1f2430"
DIM = "#5c6773"
CREAM = "#e6e1cf"
ACCENT = "#39bae6"
BLADE = "#86b300"
EMBER = "#ffb454"
GLOW = "#59c2ff"

# JetBrains Mono ships with charted's font metrics, so text layout is exact
# and no fallback warning is emitted.
_FONT = "JetBrains Mono"


def _theme(colors: list[str] | None = None) -> Theme:
    return Theme(
        colors=colors or [ACCENT, BLADE, EMBER, GLOW],
        background_color=INK,
        root_color=DIM,
        grid_color=DIVIDE,
        title_color=CREAM,
        title_font_family=_FONT,
        legend_font_family=_FONT,
        # Legends are unused (all charts render with legend="none"), but charted
        # enforces a WCAG AA contrast floor on this colour, so use the light one.
        legend_font_color=CREAM,
        data_label_color=CREAM,
    )


def svg_daily_chart(
    data: list[dict],
    width: int = 700,
    height: int = 220,
    title: str = "",
) -> str:
    """Vertical bar chart of daily counts.

    ``data`` is a list of ``{"date": "YYYY-MM-DD", "count": int}`` in
    reverse-chronological order (newest first), matching the previous API.
    """
    if not data:
        return ""

    data = list(reversed(data))
    if len(data) < 2:
        return ""

    labels = [d["date"][5:] for d in data]  # MM-DD
    values = [d["count"] for d in data]

    chart = ColumnChart(
        data=[values],
        labels=labels,
        title=title or None,
        width=width,
        height=height,
        theme=_theme(),
    )
    return _render(chart)


def svg_hbar_chart(
    data: list[dict],
    label_key: str,
    value_key: str,
    width: int = 700,
    height: int | None = None,
    title: str = "",
    bar_color: str = ACCENT,
    max_label_w: int = 160,
) -> str:
    """Horizontal bar chart from a list of dicts."""
    if not data:
        return ""

    labels = [str(d[label_key]) for d in data]
    values = [d[value_key] for d in data]
    h = height or (72 + len(data) * 28)

    chart = BarChart(
        data=[values],
        labels=labels,
        title=title or None,
        width=width,
        height=h,
        theme=_theme([bar_color]),
        value_labels=True,
        category_label_max_width=max_label_w,
    )
    return _render(chart)


def svg_multi_hbar(
    datasets: list[tuple[str, list[dict], str]],
    label_key: str,
    value_key: str,
    width: int = 700,
    height: int | None = None,
    title: str = "",
) -> str:
    """Horizontal bar chart built from several ``(name, rows, color)`` groups.

    The groups are flattened into a single ordered bar chart, preserving the
    behaviour of the previous implementation while letting ``charted`` handle
    all layout.
    """
    if not datasets:
        return ""

    labels: list[str] = []
    values: list[float] = []
    for _name, rows, _color in datasets:
        for d in rows:
            labels.append(str(d[label_key]))
            values.append(d[value_key])

    if not values:
        return ""

    h = height or (72 + len(values) * 26)

    chart = BarChart(
        data=[values],
        labels=labels,
        title=title or None,
        width=width,
        height=h,
        theme=_theme([EMBER]),
        value_labels=True,
    )
    return _render(chart)
