"""Chart geometry helpers for the BI dashboards.

Pure functions that turn numbers into ready-to-render SVG geometry (path
strings, gridlines, legends). All trig/scaling is done here so the Django
templates stay dumb (`{{ seg.d }}` etc.). Used by the on-screen dashboards; the
PDF uses reportlab.graphics natively.
"""
from __future__ import annotations

import math

# Purposeful, accessible categorical palette (blue-led, distinct hues).
PALETTE = [
    "#3B82F6", "#8B5CF6", "#10B981", "#F59E0B",
    "#EF4444", "#06B6D4", "#EC4899", "#84CC16",
    "#6366F1", "#F97316", "#14B8A6", "#A855F7",
]
ACCENT = "#3B82F6"
GRID = "#1e293b"
AXIS = "#64748b"


def _fmt(v, money=False):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0
    if money:
        if abs(v) >= 1000:
            return f"£{v/1000:.1f}k"
        return f"£{v:,.0f}"
    if abs(v) >= 1000:
        return f"{v/1000:.1f}k"
    return f"{v:,.0f}"


def line_chart(labels, values, *, money=False, w=760, h=240):
    """Area+line trend chart geometry."""
    pad_l, pad_r, pad_t, pad_b = 46, 14, 14, 28
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    values = [float(v or 0) for v in values]
    n = len(values)
    mx = max(values) if values and max(values) > 0 else 1.0

    def px(i):
        return pad_l + (plot_w * (i / (n - 1)) if n > 1 else plot_w / 2)

    def py(v):
        return pad_t + plot_h - (v / mx) * plot_h

    pts = [(px(i), py(v)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    if pts:
        area = (
            f"M {pts[0][0]:.1f},{pad_t + plot_h:.1f} "
            + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
            + f" L {pts[-1][0]:.1f},{pad_t + plot_h:.1f} Z"
        )
    else:
        area = ""

    # 4 horizontal gridlines + y labels
    grid = []
    for g in range(5):
        gv = mx * g / 4
        gy = pad_t + plot_h - (gv / mx) * plot_h
        grid.append({
            "y": round(gy, 1), "label": _fmt(gv, money),
            "x1": pad_l, "x2": pad_l + plot_w,
            "label_x": pad_l - 6, "label_y": round(gy + 3, 1),
        })

    # x labels: show ~6 evenly spaced to avoid crowding
    xlabels = []
    step = max(1, n // 6)
    for i, lbl in enumerate(labels):
        if i % step == 0 or i == n - 1:
            xlabels.append({"x": round(px(i), 1), "label": lbl})

    dots = [{"x": round(x, 1), "y": round(y, 1)} for x, y in pts]
    return {
        "w": w, "h": h, "line": line, "area": area, "grid": grid,
        "xlabels": xlabels, "xlabel_y": h - 8, "dots": dots,
        "baseline": round(pad_t + plot_h, 1),
    }


def donut_chart(rows, *, size=190, thickness=30):
    """Part-to-whole donut. rows = [[label, value], ...]."""
    cx = cy = size / 2
    r = (size - thickness) / 2
    data = [(str(l), float(v or 0)) for l, v in rows if float(v or 0) > 0]
    total = sum(v for _l, v in data) or 1.0
    segs = []
    a0 = -math.pi / 2  # start at top
    for i, (label, value) in enumerate(data):
        frac = value / total
        a1 = a0 + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        d = f"M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}"
        segs.append({
            "d": d, "color": PALETTE[i % len(PALETTE)],
            "label": label, "value": int(value), "pct": f"{frac * 100:.0f}%",
        })
        a0 = a1
    return {
        "size": size, "cx": cx, "cy": cy, "r": r, "thickness": thickness,
        "total_y": cy - 2, "caption_y": cy + 15,
        "segments": segs, "total": int(sum(v for _l, v in data)),
        "empty": not data,
    }


def bar_chart(rows, *, w=760, h=240, money=False):
    """Vertical column chart for categorical comparison. rows = [[label, value]]."""
    pad_l, pad_r, pad_t, pad_b = 46, 14, 14, 30
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    data = [(str(l), float(v or 0)) for l, v in rows]
    mx = max((v for _l, v in data), default=0) or 1.0
    n = len(data) or 1
    gap = 10
    bw = max(6, (plot_w - gap * (n + 1)) / n)
    bars = []
    for i, (label, value) in enumerate(data):
        bh = (value / mx) * plot_h
        x = pad_l + gap + i * (bw + gap)
        y = pad_t + plot_h - bh
        bars.append({
            "x": round(x, 1), "y": round(y, 1), "w": round(bw, 1), "h": round(bh, 1),
            "label": label, "value": _fmt(value, money), "color": PALETTE[i % len(PALETTE)],
        })
    grid = []
    for g in range(5):
        gv = mx * g / 4
        gy = pad_t + plot_h - (gv / mx) * plot_h
        grid.append({"y": round(gy, 1), "label": _fmt(gv, money), "x1": pad_l, "x2": pad_l + plot_w})
    return {"w": w, "h": h, "bars": bars, "grid": grid, "baseline": round(pad_t + plot_h, 1)}
