import math

INK = "#0a0e14"
DIVIDE = "#1f2430"
DIM = "#5c6773"
CREAM = "#e6e1cf"
ACCENT = "#39bae6"
BLADE = "#86b300"
EMBER = "#ffb454"
GLOW = "#59c2ff"

MONO = "'IBM Plex Mono', 'Courier New', Courier, monospace"


def _esc(text: str | int | float) -> str:
    s = str(text)
    for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        s = s.replace(a, b)
    return s


def _svg_frame(width: int, height: int) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="display:block;width:100%;height:auto;font-family:{MONO}">'
        f'<rect width="{width}" height="{height}" fill="{INK}" />'
    )


def svg_daily_chart(
    data: list[dict],
    width: int = 700,
    height: int = 220,
    title: str = "",
) -> str:
    if not data:
        return ""

    data = list(reversed(data))
    n = len(data)
    if n < 2:
        return ""

    margin = {"t": 32, "r": 16, "b": 40, "l": 52}
    cw = width - margin["l"] - margin["r"]
    ch = height - margin["t"] - margin["b"]

    vals = [d["count"] for d in data]
    mx = max(vals) or 1

    y_ticks = 5
    y_step = max(1, math.ceil(mx / y_ticks / 10) * 10)
    y_max = y_step * y_ticks

    bars: list[str] = []
    labels_x: list[str] = []
    labels_alt: list[str] = []
    grid: list[str] = []

    for yi in range(y_ticks + 1):
        yv = yi * y_step
        yy = margin["t"] + ch - (yv / y_max * ch)
        grid.append(
            f'<line x1="{margin["l"]}" y1="{yy:.1f}" '
            f'x2="{margin["l"] + cw}" y2="{yy:.1f}" '
            f'stroke="{DIVIDE}" stroke-width="1" />'
        )
        labels_alt.append(
            f'<text x="{margin["l"] - 6}" y="{yy + 3.5}" '
            f'fill="{DIM}" font-size="10" text-anchor="end">{yv}</text>'
        )

    bar_w = max(4, cw / n - 2)
    for i, d in enumerate(data):
        v = d["count"]
        bx = margin["l"] + (i / n) * cw + (cw / n - bar_w) / 2
        bh = (v / y_max) * ch
        by = margin["t"] + ch - bh
        bars.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" '
            f'height="{max(1, bh):.1f}" fill="{ACCENT}" rx="1" />'
        )

    # X-axis labels: show every Nth label to avoid crowding
    label_step = max(1, n // 12)
    for i, d in enumerate(data):
        if i % label_step == 0 or i == n - 1:
            label = d["date"][5:]  # MM-DD
            lx = margin["l"] + (i / n) * cw + (cw / n) / 2
            labels_x.append(
                f'<text x="{lx:.1f}" y="{height - margin["b"] + 16}" '
                f'fill="{DIM}" font-size="9" text-anchor="end" '
                f'transform="rotate(-40, {lx:.1f}, {height - margin["b"] + 16})">'
                f"{_esc(label)}</text>"
            )

    return (
        f"{_svg_frame(width, height)}"
        f'<text x="{margin["l"]}" y="18" fill="{CREAM}" font-size="12" '
        f'font-weight="bold">{_esc(title)}</text>'
        f"{''.join(grid)}"
        f"{''.join(labels_alt)}"
        f"{''.join(bars)}"
        f"{''.join(labels_x)}"
        f'<line x1="{margin["l"]}" y1="{margin["t"] + ch}" '
        f'x2="{margin["l"] + cw}" y2="{margin["t"] + ch}" '
        f'stroke="{DIM}" stroke-width="1" />'
        f"</svg>"
    )


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
    if not data:
        return ""

    n = len(data)
    row_h = 28
    pad_t = 32
    pad_b = 12
    pad_l = max_label_w
    pad_r = 60

    bar_h = max(6, row_h - 8)
    h = height or (pad_t + pad_b + n * row_h)

    cw = width - pad_l - pad_r
    ch = h - pad_t - pad_b

    vals = [d[value_key] for d in data]
    mx = max(vals) or 1

    grid: list[str] = []
    bars: list[str] = []
    labels: list[str] = []
    values: list[str] = []

    for yi in range(4):
        xx = pad_l + cw * yi / 3
        grid.append(
            f'<line x1="{xx:.1f}" y1="{pad_t}" '
            f'x2="{xx:.1f}" y2="{pad_t + ch}" '
            f'stroke="{DIVIDE}" stroke-width="1" />'
        )

    for i, d in enumerate(data):
        v = d[value_key]
        by = pad_t + i * row_h + (row_h - bar_h) / 2
        bw = (v / mx) * cw
        label = _esc(str(d[label_key]))

        bars.append(
            f'<rect x="{pad_l}" y="{by:.1f}" width="{max(2, bw):.1f}" '
            f'height="{bar_h}" fill="{bar_color}" rx="1" />'
        )
        labels.append(
            f'<text x="{pad_l - 6}" y="{by + bar_h / 2 + 3.5}" '
            f'fill="{DIM}" font-size="10" text-anchor="end">'
            f"{label}</text>"
        )
        values.append(
            f'<text x="{pad_l + max(2, bw) + 6}" y="{by + bar_h / 2 + 3.5}" '
            f'fill="{CREAM}" font-size="10">{v}</text>'
        )

    return (
        f"{_svg_frame(width, h)}"
        f'<text x="{pad_l}" y="18" fill="{CREAM}" font-size="12" '
        f'font-weight="bold">{_esc(title)}</text>'
        f"{''.join(grid)}"
        f"{''.join(bars)}"
        f"{''.join(labels)}"
        f"{''.join(values)}"
        f'<line x1="{pad_l}" y1="{pad_t}" '
        f'x2="{pad_l}" y2="{pad_t + ch}" '
        f'stroke="{DIM}" stroke-width="1" />'
        f'<line x1="{pad_l}" y1="{pad_t + ch}" '
        f'x2="{pad_l + cw}" y2="{pad_t + ch}" '
        f'stroke="{DIM}" stroke-width="1" />'
        f"</svg>"
    )


def svg_multi_hbar(
    datasets: list[tuple[str, list[dict], str]],
    label_key: str,
    value_key: str,
    width: int = 700,
    height: int | None = None,
    title: str = "",
) -> str:
    if not datasets:
        return ""

    pad_t = 32
    pad_b = 12
    pad_l = 160
    pad_r = 60
    row_h = 24
    group_gap = 12

    total_rows = sum(len(d) for _, d, _ in datasets)
    n_groups = len(datasets)
    h = height or (pad_t + pad_b + total_rows * row_h + (n_groups - 1) * group_gap)

    cw = width - pad_l - pad_r
    ch = h - pad_t - pad_b

    all_vals = [d[value_key] for _, dd, _ in datasets for d in dd]
    mx = max(all_vals) or 1

    colors = [ACCENT, BLADE, EMBER, GLOW]

    grid: list[str] = []
    bars: list[str] = []
    labels: list[str] = []
    values: list[str] = []

    for yi in range(4):
        xx = pad_l + cw * yi / 3
        grid.append(
            f'<line x1="{xx:.1f}" y1="{pad_t}" '
            f'x2="{xx:.1f}" y2="{pad_t + ch}" '
            f'stroke="{DIVIDE}" stroke-width="1" />'
        )

    cur_y = pad_t
    for gi, (group_label, group_data, color) in enumerate(datasets):
        color = color or colors[gi % len(colors)]
        bar_h = max(4, row_h - 6)
        for i, d in enumerate(group_data):
            v = d[value_key]
            by = cur_y + i * row_h + (row_h - bar_h) / 2
            bw = (v / mx) * cw
            label = _esc(str(d[label_key]))

            bars.append(
                f'<rect x="{pad_l}" y="{by:.1f}" width="{max(2, bw):.1f}" '
                f'height="{bar_h}" fill="{color}" rx="1" />'
            )
            labels.append(
                f'<text x="{pad_l - 6}" y="{by + bar_h / 2 + 3.5}" '
                f'fill="{DIM}" font-size="10" text-anchor="end">'
                f"{label}</text>"
            )
            values.append(
                f'<text x="{pad_l + max(2, bw) + 6}" y="{by + bar_h / 2 + 3.5}" '
                f'fill="{CREAM}" font-size="10">{v}</text>'
            )
        cur_y += len(group_data) * row_h + group_gap

    return (
        f"{_svg_frame(width, h)}"
        f'<text x="{pad_l}" y="18" fill="{CREAM}" font-size="12" '
        f'font-weight="bold">{_esc(title)}</text>'
        f"{''.join(grid)}"
        f"{''.join(bars)}"
        f"{''.join(labels)}"
        f"{''.join(values)}"
        f'<line x1="{pad_l}" y1="{pad_t}" '
        f'x2="{pad_l}" y2="{pad_t + ch}" '
        f'stroke="{DIM}" stroke-width="1" />'
        f'<line x1="{pad_l}" y1="{pad_t + ch}" '
        f'x2="{pad_l + cw}" y2="{pad_t + ch}" '
        f'stroke="{DIM}" stroke-width="1" />'
        f"</svg>"
    )
