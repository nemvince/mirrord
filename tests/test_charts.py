import warnings
import xml.dom.minidom as minidom

from app.charts import svg_daily_chart, svg_hbar_chart, svg_multi_hbar


def _assert_static_svg(svg: str) -> None:
    """A rendered chart must be non-empty, valid XML, and contain no JS."""
    assert svg
    assert svg.lstrip().startswith("<svg")
    lower = svg.lower()
    assert "<script" not in lower
    assert "onload" not in lower
    assert "onclick" not in lower
    minidom.parseString(svg.encode())


def _daily(n: int) -> list[dict]:
    return [
        {"date": f"2026-01-{i:02d}", "count": (i * 7) % 23} for i in range(1, n + 1)
    ]


def test_daily_chart_renders_static_svg() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # fail on font-fallback warnings
        svg = svg_daily_chart(_daily(30), title="Downloads per Day")
    _assert_static_svg(svg)


def test_daily_chart_empty_returns_empty() -> None:
    assert svg_daily_chart([]) == ""


def test_daily_chart_single_point_returns_empty() -> None:
    assert svg_daily_chart([{"date": "2026-01-01", "count": 5}]) == ""


def test_hbar_chart_renders_static_svg() -> None:
    data = [{"geocode": "US", "count": 120}, {"geocode": "DE", "count": 45}]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        svg = svg_hbar_chart(
            data,
            label_key="geocode",
            value_key="count",
            title="Downloads by Geography",
            bar_color="#86b300",
            max_label_w=80,
        )
    _assert_static_svg(svg)


def test_hbar_chart_zero_values() -> None:
    svg = svg_hbar_chart(
        [{"geocode": "\u2014", "count": 0}],
        label_key="geocode",
        value_key="count",
    )
    _assert_static_svg(svg)


def test_hbar_chart_empty_returns_empty() -> None:
    assert svg_hbar_chart([], label_key="a", value_key="b") == ""


def test_multi_hbar_renders_static_svg() -> None:
    datasets = [
        ("Cool Plugin", [{"slug": "cool", "count": 80}], "#ffb454"),
        ("Other", [{"slug": "other", "count": 40}], "#ffb454"),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        svg = svg_multi_hbar(
            datasets,
            label_key="slug",
            value_key="count",
            title="Downloads per Mirror",
        )
    _assert_static_svg(svg)


def test_multi_hbar_empty_returns_empty() -> None:
    assert svg_multi_hbar([], label_key="a", value_key="b") == ""


def test_multi_hbar_empty_rows_returns_empty() -> None:
    assert svg_multi_hbar([("g", [], "#fff")], label_key="a", value_key="b") == ""
