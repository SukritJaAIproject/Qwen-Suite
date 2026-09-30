"""Draw one chart PNG with matplotlib (added 2026-09-30).

Runs under the user's Anaconda Python, because webui-venv has no matplotlib:
    python chart_worker.py <spec.json> <result.json>
exit 0 = drawn, 2 = bad data, 3 = matplotlib missing or broken (the launcher tries the next
Python), 1 = anything else. charts.py validates and normalises the data first, so the spec is:
    {type: bar|line|pie|scatter, title, x_label, y_label, categories: [..],
     series: [{name, values: [..]}] or, for scatter, [{name, x: [..], y: [..]}],
     output, font_regular, font_bold, dpi, size: [w_in, h_in]}

Kept free of local-tools imports and of Python 3.9+ syntax: anaconda base is Python 3.8.
Colours: the categorical order and chart chrome of the suite's data-viz palette (identity by
fixed slot order, recessive grid, text in ink colours, never in the series colour).
"""
from __future__ import annotations

import json
import math
import sys

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))


def _fmt(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if abs(value) >= 1000:
        return "{:,.0f}".format(value)
    if float(value).is_integer():
        return "{:.0f}".format(value)
    return ("{:.2f}".format(value)).rstrip("0").rstrip(".")


def _nan(values):
    return [float("nan") if v is None else v for v in values]


def _style_axes(ax, horizontal_grid=True):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", colors=MUTED, labelcolor=INK_2, labelsize=16, length=0, pad=6)
    if horizontal_grid:
        ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


def _category_ticks(ax, categories):
    ax.set_xticks(list(range(len(categories))))
    longest = max([len(str(c)) for c in categories] or [0])
    if longest * len(categories) > 70:
        ax.set_xticklabels(categories, rotation=30, ha="right")
    else:
        ax.set_xticklabels(categories)


def _end_labels(ax, ends, dpi, count):
    """Series names at the line ends, nudged apart vertically so equal values do not overlap."""
    ax.set_xlim(-0.3, count - 1 + 0.9)  # room on the right for the names
    ax.autoscale_view()
    gap = 15 * dpi / 72.0 * 1.15  # one label height in pixels
    placed = []
    for name, x, y in ends:
        placed.append([ax.transData.transform((x, y))[1], name, x, y])
    placed.sort(key=lambda item: item[0])
    for k in range(1, len(placed)):
        if placed[k][0] - placed[k - 1][0] < gap:
            placed[k][0] = placed[k - 1][0] + gap
    for pixel_y, name, x, y in placed:
        shift = (pixel_y - ax.transData.transform((x, y))[1]) * 72.0 / dpi
        ax.annotate(name, (x, y), xytext=(8, shift), textcoords="offset points", va="center",
                    fontsize=15, color=INK_2)


def draw(spec, plt, FuncFormatter):
    kind = spec["type"]
    series = spec.get("series") or []
    categories = spec.get("categories") or []
    size = spec.get("size") or [10, 6]
    dpi = int(spec.get("dpi") or 150)
    fig, ax = plt.subplots(figsize=(float(size[0]), float(size[1])), dpi=dpi)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    many = len(series) >= 2

    if kind == "bar":
        count = len(series)
        width = min(0.8 / count, 0.6)
        labelled = count <= 3 and len(categories) <= 12
        for i, s in enumerate(series):
            offset = (i - (count - 1) / 2.0) * width
            xs = [x + offset for x in range(len(categories))]
            values = _nan(s["values"])
            bars = ax.bar(xs, values, width=width * 0.9, color=PALETTE[i], label=s.get("name") or None, zorder=3)
            if labelled:
                for rect, v in zip(bars, s["values"]):
                    if v is None:
                        continue
                    ax.annotate(_fmt(v), (rect.get_x() + rect.get_width() / 2.0, rect.get_height()),
                                xytext=(0, 4 if v >= 0 else -4), textcoords="offset points",
                                ha="center", va="bottom" if v >= 0 else "top", fontsize=15, color=INK_2)
        _category_ticks(ax, categories)
        ax.axhline(0, color=AXIS, linewidth=1, zorder=2)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _fmt(v)))
        _style_axes(ax)
    elif kind == "line":
        last = len(categories) - 1
        ends = []
        for i, s in enumerate(series):
            ax.plot(list(range(len(categories))), _nan(s["values"]), color=PALETTE[i], linewidth=2,
                    marker="o", markersize=6, label=s.get("name") or None, zorder=3)
            end = next((j for j in range(last, -1, -1) if s["values"][j] is not None), None)
            if many and len(series) <= 4 and end is not None and s.get("name"):
                ends.append((str(s["name"]), end, s["values"][end]))
        _category_ticks(ax, categories)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _fmt(v)))
        _style_axes(ax)
        if ends:
            _end_labels(ax, ends, dpi, len(categories))
    elif kind == "pie":
        values = series[0]["values"]
        total = float(sum(values)) or 1.0
        wedges, _texts = ax.pie(values, labels=None, colors=PALETTE[:len(values)], startangle=90, counterclock=False,
                                wedgeprops={"edgecolor": SURFACE, "linewidth": 2})
        for wedge, v in zip(wedges, values):
            if v / total < 0.02:
                continue
            # percentages sit outside the wedge in ink colour: white text fails on the light slots
            angle = math.radians((wedge.theta1 + wedge.theta2) / 2.0)
            x, y = 1.14 * math.cos(angle), 1.14 * math.sin(angle)
            ax.annotate("{:.1f}%".format(100.0 * v / total), (x, y), ha="left" if x >= 0 else "right",
                        va="center", fontsize=16, color=INK_2)
        legend = ax.legend(wedges, categories, frameon=False, loc="center left", bbox_to_anchor=(1.08, 0.5), fontsize=16)
        for text in legend.get_texts():
            text.set_color(INK_2)
        ax.axis("equal")
    elif kind == "scatter":
        for i, s in enumerate(series):
            ax.scatter(s["x"], s["y"], s=64, color=PALETTE[i], edgecolors=SURFACE, linewidths=1.2,
                       label=s.get("name") or None, zorder=3)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: _fmt(v)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _fmt(v)))
        _style_axes(ax)
        ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    else:
        raise ValueError("unknown chart type: {}".format(kind))

    if kind != "pie" and many:
        legend = ax.legend(frameon=False, fontsize=16, loc="best")
        for text in legend.get_texts():
            text.set_color(INK_2)
    if spec.get("title"):
        ax.set_title(spec["title"], loc="left", fontsize=24, fontweight="bold", color=INK, pad=14)
    if spec.get("x_label") and kind != "pie":
        ax.set_xlabel(spec["x_label"], fontsize=18, color=INK_2)
    if spec.get("y_label") and kind != "pie":
        ax.set_ylabel(spec["y_label"], fontsize=18, color=INK_2)
    fig.tight_layout()
    fig.savefig(spec["output"], dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


def main(argv):
    if len(argv) != 3:
        print("usage: chart_worker.py <spec.json> <result.json>", file=sys.stderr)
        return 1
    spec_path, result_path = argv[1], argv[2]
    try:
        with open(spec_path, encoding="utf-8") as fh:
            spec = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - reported back to the launcher
        _write(result_path, {"ok": False, "error": "spec unreadable: {}".format(exc)})
        return 2
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.ticker import FuncFormatter
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "stage": "import", "error": "{}: {}".format(type(exc).__name__, exc)})
        return 3
    warnings = []
    font_name = None
    for key in ("font_regular", "font_bold"):
        path = spec.get(key)
        if not path:
            continue
        try:
            font_manager.fontManager.addfont(path)
            if key == "font_regular":
                font_name = font_manager.FontProperties(fname=path).get_name()
        except Exception as exc:  # noqa: BLE001
            warnings.append("font {} not loaded: {}".format(path, exc))
    if font_name:
        plt.rcParams["font.family"] = [font_name, "DejaVu Sans"]
    else:
        warnings.append("no Thai font: Thai labels may show as boxes")
    plt.rcParams["axes.unicode_minus"] = False  # TH Sarabun has no U+2212 minus sign
    plt.rcParams["font.size"] = 16
    try:
        draw(spec, plt, FuncFormatter)
    except ValueError as exc:
        _write(result_path, {"ok": False, "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)})
        return 1
    _write(result_path, {"ok": True, "font": font_name, "warnings": warnings,
                         "matplotlib": matplotlib.__version__, "python": sys.version.split()[0]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
