"""Builds the Minto spending report as a PDF.

Pure functions: transactions in, PDF bytes out. Nothing here touches the
database or the disk beyond reading the bundled font and logo, so the report
is generated on the fly and never stored.
"""
import io
import os
import math
from collections import defaultdict
from datetime import date, timedelta

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.graphics.charts.barcharts import VerticalBarChart, HorizontalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "static", "fonts")
LOGO = os.path.join(HERE, "static", "icons", "icon-192.png")

def _register_fonts():
    """Use DejaVu Sans (it has the rupee sign). Looks in the bundled
    static/fonts folder first, then common system locations. If neither has
    it, the report still builds with Helvetica and writes "Rs" for the rupee,
    so a missing font file can never stop the app from starting."""
    candidates = [
        FONT_DIR,
        os.path.join(HERE, "static"),  # in case the fonts were dropped in static/ directly
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
        "/usr/share/fonts/TTF",
    ]
    for folder in candidates:
        regular = os.path.join(folder, "DejaVuSans.ttf")
        bold = os.path.join(folder, "DejaVuSans-Bold.ttf")
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("MintoSans", regular))
                pdfmetrics.registerFont(TTFont("MintoSans-Bold", bold))
                pdfmetrics.registerFontFamily(
                    "MintoSans", normal="MintoSans", bold="MintoSans-Bold",
                    italic="MintoSans", boldItalic="MintoSans-Bold",
                )
                return "\u20b9"
            except Exception:
                pass
    pdfmetrics.registerFont(pdfmetrics.Font("MintoSans", "Helvetica", "WinAnsiEncoding"))
    pdfmetrics.registerFont(pdfmetrics.Font("MintoSans-Bold", "Helvetica-Bold", "WinAnsiEncoding"))
    pdfmetrics.registerFontFamily(
        "MintoSans", normal="MintoSans", bold="MintoSans-Bold",
        italic="MintoSans", boldItalic="MintoSans-Bold",
    )
    return "Rs "


CUR = _register_fonts()

INK = colors.HexColor("#17181D")
MUTED = colors.HexColor("#6B6E76")
LINE = colors.HexColor("#E4E1D9")
CARD = colors.HexColor("#F6F4EE")
GREEN = colors.HexColor("#1F6F50")
CORAL = colors.HexColor("#C8553D")
OTHER = colors.HexColor("#A8ADB3")
PALETTE = [
    colors.HexColor(c) for c in
    ("#1F6F50", "#E0A030", "#3B7EA1", "#C8553D", "#7FA37A", "#5B6770", "#C9B79C", "#2A9D8F")
]

NON_FLOW = ("transfer", "lending", "trip_expense_payment", "trip_settlement")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


# ----------------------------------------------------------------- formatting

def _group(whole):
    """Indian digit grouping: 1234567 -> 12,34,567."""
    s = str(int(whole))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def inr0(x):
    x = round(float(x))
    return ("-" if x < 0 else "") + CUR + _group(abs(x))


def inr2(x):
    x = round(float(x), 2)
    whole, frac = f"{abs(x):.2f}".split(".")
    return ("-" if x < 0 else "") + CUR + _group(whole) + "." + frac


def compact(v):
    v = float(v)
    for size, suffix in ((1e7, "Cr"), (1e5, "L"), (1e3, "k")):
        if abs(v) >= size:
            n = f"{v / size:.1f}".rstrip("0").rstrip(".")
            return n + suffix
    return f"{v:.0f}"


def pretty(label):
    return (label or "other").replace("_", " ").strip().title()


def fdate(d):
    return d.strftime("%d-%b-%Y").upper()


def _nice_axis(max_value):
    """(axis max, step) so the value axis shows about 4 clean ticks."""
    if max_value <= 0:
        return 1, 1
    raw = max_value / 4
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    return step * math.ceil(max_value / step), step


# ---------------------------------------------------------------------- stats

def _day(t):
    return date.fromisoformat(str(t.get("transaction_date"))[:10])


def compute_stats(rows, d_from, d_to, prev_spend=None):
    days = (d_to - d_from).days + 1
    total_in = total_out = invested = 0.0
    spend = 0.0
    daily = defaultdict(float)
    by_cat = defaultdict(float)
    cat_count = defaultdict(int)
    by_source = defaultdict(float)
    by_weekday = [0.0] * 7
    monthly = defaultdict(lambda: [0.0, 0.0])
    expenses = []
    lending = defaultdict(lambda: [0.0, 0.0])  # person -> [lent, repaid]

    for t in rows:
        amount = float(t.get("amount") or 0)
        category = t.get("category") or ""
        direction = t.get("direction") or ""
        d = _day(t)

        if category == "lending":
            who = (t.get("counterparty") or "Unnamed").strip() or "Unnamed"
            if direction == "out":
                lending[who][0] += amount
            elif direction == "in":
                lending[who][1] += amount

        if category not in NON_FLOW:
            key = (d.year, d.month)
            if direction == "in":
                total_in += amount
                monthly[key][0] += amount
            elif direction == "out":
                total_out += amount
                monthly[key][1] += amount
                src = (t.get("user_sources") or {}).get("name") or "No account"
                by_source[src] += amount

        if category == "investment" and direction == "out":
            invested += amount

        if category == "expense":
            spend += amount
            daily[d] += amount
            label = pretty(t.get("expense_category"))
            by_cat[label] += amount
            cat_count[label] += 1
            by_weekday[d.weekday()] += amount
            expenses.append((amount, d, t))

    # How many of each weekday fall inside the range, for a fair average.
    weekday_days = [0] * 7
    cursor = d_from
    while cursor <= d_to:
        weekday_days[cursor.weekday()] += 1
        cursor += timedelta(days=1)
    weekday_avg = [by_weekday[i] / weekday_days[i] if weekday_days[i] else 0.0 for i in range(7)]

    expenses.sort(key=lambda e: e[0], reverse=True)
    top_day = max(daily.items(), key=lambda kv: kv[1]) if daily else None
    change = None
    if prev_spend and prev_spend > 0:
        change = (spend - prev_spend) / prev_spend * 100

    return {
        "days": days,
        "tx_count": len(rows),
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "savings_rate": ((total_in - total_out) / total_in * 100) if total_in > 0 else None,
        "invested": invested,
        "spend": spend,
        "spend_count": len(expenses),
        "avg_daily": spend / days if days else 0.0,
        "active_days": len(daily),
        "avg_per_tx": spend / len(expenses) if expenses else 0.0,
        "daily": daily,
        "by_cat": sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True),
        "cat_count": cat_count,
        "by_source": sorted(by_source.items(), key=lambda kv: kv[1], reverse=True),
        "weekday_avg": weekday_avg,
        "monthly": dict(sorted(monthly.items())),
        "top_expenses": expenses[:5],
        "top_day": top_day,
        "largest": expenses[0] if expenses else None,
        "lending": sorted(lending.items(), key=lambda kv: kv[1][0] - kv[1][1], reverse=True),
        "prev_spend": prev_spend,
        "change": change,
    }


# --------------------------------------------------------------------- styles

def _style(name, **kw):
    base = dict(fontName="MintoSans", fontSize=9, leading=12, textColor=INK)
    base.update(kw)
    return ParagraphStyle(name, **base)


S_TITLE = _style("title", fontName="MintoSans-Bold", fontSize=20, leading=24)
S_SUB = _style("sub", fontSize=9, leading=12, textColor=MUTED)
S_RIGHT = _style("right", fontSize=9, leading=12, alignment=TA_RIGHT)
S_RIGHT_MUTED = _style("rightm", fontSize=8, leading=11, alignment=TA_RIGHT, textColor=MUTED)
S_H2 = _style("h2", fontName="MintoSans-Bold", fontSize=12.5, leading=16, spaceBefore=14, spaceAfter=6)
S_NOTE = _style("note", fontSize=8, leading=11, textColor=MUTED)
S_BODY = _style("body", fontSize=9, leading=13)
S_CELL = _style("cell", fontSize=8.5, leading=11)
S_CELL_R = _style("cellr", fontSize=8.5, leading=11, alignment=TA_RIGHT)
S_HEAD = _style("head", fontName="MintoSans-Bold", fontSize=7.5, leading=10, textColor=MUTED)
S_HEAD_R = _style("headr", fontName="MintoSans-Bold", fontSize=7.5, leading=10, textColor=MUTED, alignment=TA_RIGHT)


def _esc(text):
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------------- cards

def _card_cell(label, value, sub="", color=INK):
    return [
        Paragraph(_esc(label).upper(), _style("cl", fontSize=6.8, leading=9, textColor=MUTED)),
        Paragraph(_esc(value), _style("cv", fontName="MintoSans-Bold", fontSize=14.5, leading=19, textColor=color)),
        Paragraph(_esc(sub) if sub else "&nbsp;", _style("cs", fontSize=7, leading=9, textColor=MUTED)),
    ]


def _card_row(cards):
    gap = 5 * mm
    n = len(cards)
    w = (CONTENT_W - gap * (n - 1)) / n
    cells, widths = [], []
    for i, c in enumerate(cards):
        cells.append(_card_cell(*c))
        widths.append(w)
        if i < n - 1:
            cells.append("")
            widths.append(gap)
    t = Table([cells], colWidths=widths)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 0),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]
    for i in range(0, len(cells), 2):
        style += [
            ("BACKGROUND", (i, 0), (i, 0), CARD),
            ("LEFTPADDING", (i, 0), (i, 0), 9), ("RIGHTPADDING", (i, 0), (i, 0), 9),
            ("TOPPADDING", (i, 0), (i, 0), 8), ("BOTTOMPADDING", (i, 0), (i, 0), 8),
            ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ]
    t.setStyle(TableStyle(style))
    return t


# --------------------------------------------------------------------- charts

def _axis_style(axis, size=7):
    axis.labels.fontName = "MintoSans"
    axis.labels.fontSize = size
    axis.labels.fillColor = MUTED
    axis.strokeColor = LINE
    axis.visibleTicks = 0 if hasattr(axis, "visibleTicks") else None


def _vbar(width, height, series, names, colors_by_series, avg_line=None, avg_label=None,
          value_labels=False, highlight_index=None):
    """Vertical bar chart (one or more series) with clean axes."""
    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x, chart.y = 38, 22
    chart.width, chart.height = width - 46, height - 40
    chart.data = series
    maxv = max((max(s) for s in series if s), default=0)
    vmax, step = _nice_axis(maxv)
    chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, vmax, step
    chart.valueAxis.labelTextFormat = lambda v: compact(v)
    chart.valueAxis.gridStrokeColor = LINE
    chart.valueAxis.gridStrokeWidth = 0.5
    chart.valueAxis.visibleGrid = 1
    chart.valueAxis.visibleAxis = 0
    chart.valueAxis.visibleTicks = 0
    chart.valueAxis.labels.fontName = "MintoSans"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fillColor = MUTED
    chart.categoryAxis.categoryNames = names
    chart.categoryAxis.labels.fontName = "MintoSans"
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.fillColor = MUTED
    chart.categoryAxis.labels.dy = -2
    chart.categoryAxis.strokeColor = LINE
    chart.categoryAxis.visibleTicks = 0
    chart.groupSpacing = 4 if len(series) == 1 else 8
    chart.barSpacing = 1
    for i, c in enumerate(colors_by_series):
        chart.bars[i].fillColor = c
        chart.bars[i].strokeColor = None
    if highlight_index is not None and len(series) == 1:
        chart.bars[(0, highlight_index)].fillColor = CORAL
    if value_labels:
        chart.barLabelFormat = lambda v: compact(v) if v else ""
        chart.barLabels.nudge = 6
        chart.barLabels.fontName = "MintoSans"
        chart.barLabels.fontSize = 6.5
        chart.barLabels.fillColor = INK
    d.add(chart)
    if avg_line is not None and vmax:
        y = chart.y + (avg_line / vmax) * chart.height
        d.add(Line(chart.x, y, chart.x + chart.width, y, strokeColor=CORAL, strokeWidth=0.9,
                   strokeDashArray=[3, 2]))
        if avg_label:
            top = height - 8
            text_w = pdfmetrics.stringWidth(avg_label, "MintoSans", 7.5)
            key_end = chart.x + chart.width - text_w - 5
            d.add(Line(key_end - 14, top + 2.5, key_end, top + 2.5,
                       strokeColor=CORAL, strokeWidth=0.9, strokeDashArray=[3, 2]))
            d.add(String(chart.x + chart.width, top, avg_label, fontName="MintoSans", fontSize=7.5,
                         fillColor=CORAL, textAnchor="end"))
    return d


def _hbar(width, items, color=GREEN):
    """Horizontal bars with the amount printed at the end of each bar."""
    n = len(items)
    height = 20 + n * 22
    d = Drawing(width, height)
    chart = HorizontalBarChart()
    label_w = 110
    chart.x, chart.y = label_w, 8
    chart.width, chart.height = width - label_w - 62, height - 16
    values = [v for _, v in items][::-1]
    chart.data = [values]
    vmax, _ = _nice_axis(max(values) if values else 0)
    chart.valueAxis.valueMin, chart.valueAxis.valueMax = 0, vmax
    chart.valueAxis.visibleAxis = 0
    chart.valueAxis.visibleLabels = 0
    chart.valueAxis.visibleTicks = 0
    chart.valueAxis.visibleGrid = 0
    chart.categoryAxis.categoryNames = [_trim(name, 22) for name, _ in items][::-1]
    chart.categoryAxis.labels.fontName = "MintoSans"
    chart.categoryAxis.labels.fontSize = 8
    chart.categoryAxis.labels.fillColor = INK
    chart.categoryAxis.labels.boxAnchor = "e"
    chart.categoryAxis.labels.dx = -6
    chart.categoryAxis.strokeColor = LINE
    chart.categoryAxis.visibleTicks = 0
    chart.bars[0].fillColor = color
    chart.bars[0].strokeColor = None
    chart.barWidth = 12
    chart.groupSpacing = 8
    chart.barLabelFormat = lambda v: inr0(v)
    chart.barLabels.nudge = 4
    chart.barLabels.boxAnchor = "w"
    chart.barLabels.fontName = "MintoSans"
    chart.barLabels.fontSize = 7.5
    chart.barLabels.fillColor = INK
    d.add(chart)
    return d


def _trim(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _time_series(stats, d_from, d_to):
    """(labels, values, avg, avg_label, unit) in day, week or month buckets."""
    daily, days = stats["daily"], stats["days"]
    if days <= 31:
        labels, values = [], []
        for i in range(days):
            d = d_from + timedelta(days=i)
            labels.append(str(d.day))
            values.append(daily.get(d, 0.0))
        avg = sum(values) / len(values)
        return labels, values, avg, f"Avg {inr0(avg)} / day", "day"
    if days <= 92:
        labels, values, i = [], [], 0
        while True:
            start = d_from + timedelta(days=7 * i)
            if start > d_to:
                break
            end = min(start + timedelta(days=6), d_to)
            total = sum(v for k, v in daily.items() if start <= k <= end)
            labels.append(f"{start.day} {start.strftime('%b')}")
            values.append(total)
            i += 1
        avg = sum(values) / len(values)
        return labels, values, avg, f"Avg {inr0(avg)} / week", "week"
    buckets = {}
    cursor = date(d_from.year, d_from.month, 1)
    while cursor <= d_to:
        buckets[(cursor.year, cursor.month)] = 0.0
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    for k, v in daily.items():
        buckets[(k.year, k.month)] += v
    labels = [date(y, m, 1).strftime("%b %y") for (y, m) in buckets]
    values = list(buckets.values())
    avg = sum(values) / len(values)
    return labels, values, avg, f"Avg {inr0(avg)} / month", "month"


def _thin(labels, per_label_pt, width):
    """Blank out labels so the rest never overlap."""
    step = max(1, math.ceil(len(labels) * per_label_pt / max(width - 60, 1)))
    return [l if i % step == 0 else "" for i, l in enumerate(labels)]


def _pie(items, size=130):
    d = Drawing(size, size)
    pie = Pie()
    pie.x = pie.y = 4
    pie.width = pie.height = size - 8
    pie.data = [v for _, v in items]
    pie.labels = None
    pie.startAngle = 90
    pie.direction = "clockwise"
    pie.slices.strokeColor = colors.white
    pie.slices.strokeWidth = 1.4
    for i, (name, _) in enumerate(items):
        pie.slices[i].fillColor = OTHER if name == "Other" else PALETTE[i % len(PALETTE)]
    d.add(pie)
    return d


def _swatch(color):
    d = Drawing(9, 9)
    d.add(Rect(0, 0, 9, 9, rx=2, ry=2, fillColor=color, strokeColor=None))
    return d


def _table(data, widths, extra=None):
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


# ------------------------------------------------------------------- the PDF

def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 12 * mm, PAGE_W - MARGIN, 12 * mm)
    canvas.setFont("MintoSans", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 8 * mm, "Minto  |  Spending report")
    canvas.drawRightString(PAGE_W - MARGIN, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_report_pdf(user_name, d_from, d_to, rows, prev_spend=None):
    s = compute_stats(rows, d_from, d_to, prev_spend)
    story = []

    # ---- Header
    logo = Image(LOGO, width=13 * mm, height=13 * mm) if os.path.exists(LOGO) else ""
    title_block = [
        Paragraph("Spending report", S_TITLE),
        Paragraph(f"{fdate(d_from)} to {fdate(d_to)}  |  {s['days']} day{'s' if s['days'] != 1 else ''}", S_SUB),
    ]
    right_block = [
        Paragraph(f"<b>{_esc(user_name)}</b>", S_RIGHT),
        Paragraph(f"Generated {fdate(date.today())}", S_RIGHT_MUTED),
    ]
    header = Table([[logo, title_block, right_block]],
                   colWidths=[16 * mm, CONTENT_W - 16 * mm - 55 * mm, 55 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, GREEN),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [header, Spacer(1, 10)]

    # ---- Stat cards
    rate = s["savings_rate"]
    net_color = GREEN if s["net"] >= 0 else CORAL
    story.append(_card_row([
        ("Money in", inr0(s["total_in"]), "Excludes transfers and lent"),
        ("Money out", inr0(s["total_out"]), "Excludes transfers and lent"),
        ("Net", inr0(s["net"]), "In minus out", net_color),
        ("Savings rate", f"{rate:.0f}%" if rate is not None else "n/a", "Net as share of money in"),
    ]))
    story.append(Spacer(1, 6))
    top_day = s["top_day"]
    largest = s["largest"]
    story.append(_card_row([
        ("Total spend", inr0(s["spend"]), f"{s['spend_count']} expense{'s' if s['spend_count'] != 1 else ''}"),
        ("Avg daily spend", inr0(s["avg_daily"]), f"Over all {s['days']} days"),
        ("Highest spend day", inr0(top_day[1]) if top_day else "n/a", fdate(top_day[0]) if top_day else "No expenses"),
        ("Largest expense", inr0(largest[0]) if largest else "n/a",
         _trim(largest[2].get("description") or pretty(largest[2].get("expense_category")), 24) if largest else ""),
    ]))
    story.append(Spacer(1, 4))

    # ---- Highlights
    bullets = []
    if s["by_cat"] and s["spend"] > 0:
        name, amt = s["by_cat"][0]
        bullets.append(f"<b>{_esc(name)}</b> is your biggest category: {inr0(amt)}, {amt / s['spend'] * 100:.0f}% of spend.")
    if s["spend"] > 0 and s["days"] >= 14:
        wa = s["weekday_avg"]
        hi = max(range(7), key=lambda i: wa[i])
        lo = min(range(7), key=lambda i: wa[i])
        if wa[hi] > 0 and hi != lo:
            bullets.append(f"You spend most on <b>{WEEKDAYS_LONG[hi]}s</b> (avg {inr0(wa[hi])}) and least on {WEEKDAYS_LONG[lo]}s (avg {inr0(wa[lo])}).")
    if s["active_days"]:
        bullets.append(f"You spent on {s['active_days']} of {s['days']} day{'s' if s['days'] != 1 else ''}. Average per expense: {inr0(s['avg_per_tx'])}.")
    if s["change"] is not None:
        direction = "up" if s["change"] > 0 else "down"
        bullets.append(f"Spend is <b>{direction} {abs(s['change']):.0f}%</b> compared with the previous {s['days']} days ({inr0(s['prev_spend'])}).")
    if s["invested"] > 0:
        bullets.append(f"You put {inr0(s['invested'])} into investments in this period.")
    if bullets:
        story.append(Paragraph("Highlights", S_H2))
        for b in bullets:
            story.append(Paragraph(f"&bull;&nbsp; {b}", S_BODY))

    # ---- Spend over time
    story.append(CondPageBreak(75 * mm))
    story.append(Paragraph("Spend over time", S_H2))
    if s["spend"] > 0:
        labels, values, avg, avg_label, unit = _time_series(s, d_from, d_to)
        per_label = 16 if unit == "day" else 34
        names = _thin(labels, per_label, CONTENT_W)
        story.append(_vbar(CONTENT_W, 175, [values], names, [GREEN], avg_line=avg, avg_label=avg_label))
        story.append(Paragraph(f"Each bar is one {unit}. The dashed line is the average.", S_NOTE))
    else:
        story.append(Paragraph("No expenses were recorded in this period.", S_NOTE))

    # ---- Category-wise spend
    if s["by_cat"] and s["spend"] > 0:
        story.append(CondPageBreak(70 * mm))
        story.append(Paragraph("Spend by category", S_H2))
        items = s["by_cat"]
        if len(items) > 8:
            other = sum(v for _, v in items[7:])
            items = items[:7] + [("Other", other)]
        rows_t = [[Paragraph("", S_HEAD), Paragraph("CATEGORY", S_HEAD), Paragraph("SPEND", S_HEAD_R),
                   Paragraph("SHARE", S_HEAD_R), Paragraph("COUNT", S_HEAD_R)]]
        for i, (name, amt) in enumerate(items):
            count = s["cat_count"].get(name, 0) if name != "Other" else sum(
                s["cat_count"].get(n, 0) for n, _ in s["by_cat"][7:])
            rows_t.append([
                _swatch(OTHER if name == "Other" else PALETTE[i % len(PALETTE)]),
                Paragraph(_esc(name), S_CELL),
                Paragraph(inr0(amt), S_CELL_R),
                Paragraph(f"{amt / s['spend'] * 100:.1f}%", S_CELL_R),
                Paragraph(str(count), S_CELL_R),
            ])
        legend_w = CONTENT_W - 135
        legend = _table(rows_t, [14, legend_w - 14 - 62 - 44 - 46, 62, 44, 46])
        block = Table([[_pie(items), legend]], colWidths=[135, legend_w])
        block.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(block)

    # ---- Weekday pattern (only meaningful over a couple of weeks or more)
    if s["spend"] > 0 and s["days"] >= 14:
        story.append(CondPageBreak(80 * mm))
        story.append(Paragraph("Average spend by weekday", S_H2))
        wa = s["weekday_avg"]
        hi = max(range(7), key=lambda i: wa[i])
        story.append(_vbar(CONTENT_W, 150, [wa], WEEKDAYS, [GREEN], value_labels=True,
                           highlight_index=hi if wa[hi] > 0 else None))
        story.append(Paragraph("Average across all matching days in the period. The highest weekday is highlighted.", S_NOTE))

    # ---- Where the money left from
    if s["by_source"]:
        items = s["by_source"][:8]
        story.append(CondPageBreak(20 * mm + len(items) * 8 * mm))
        story.append(Paragraph("Money out by account", S_H2))
        story.append(_hbar(CONTENT_W, items))

    # ---- Monthly in vs out
    if len(s["monthly"]) >= 2:
        story.append(CondPageBreak(85 * mm))
        story.append(Paragraph("Money in and out by month", S_H2))
        keys = list(s["monthly"].keys())
        names = [date(y, m, 1).strftime("%b %y") for y, m in keys]
        ins = [s["monthly"][k][0] for k in keys]
        outs = [s["monthly"][k][1] for k in keys]
        story.append(_vbar(CONTENT_W, 165, [ins, outs], names, [GREEN, CORAL], value_labels=len(keys) <= 6))
        story.append(Paragraph("Green is money in, red is money out. Transfers and lent money are not counted.", S_NOTE))

    # ---- Top expenses
    if s["top_expenses"]:
        rows_t = [[Paragraph("DATE", S_HEAD), Paragraph("WHAT", S_HEAD), Paragraph("CATEGORY", S_HEAD),
                   Paragraph("ACCOUNT", S_HEAD), Paragraph("AMOUNT", S_HEAD_R)]]
        for amt, d, t in s["top_expenses"]:
            rows_t.append([
                Paragraph(fdate(d), S_CELL),
                Paragraph(_esc(_trim(t.get("description") or "-", 30)), S_CELL),
                Paragraph(_esc(pretty(t.get("expense_category"))), S_CELL),
                Paragraph(_esc((t.get("user_sources") or {}).get("name") or "-"), S_CELL),
                Paragraph(inr2(amt), S_CELL_R),
            ])
        story.append(KeepTogether([
            Paragraph("Five largest expenses", S_H2),
            _table(rows_t, [70, CONTENT_W - 70 - 85 - 90 - 70, 85, 90, 70]),
        ]))

    # ---- Lending
    if s["lending"]:
        rows_t = [[Paragraph("PERSON", S_HEAD), Paragraph("LENT", S_HEAD_R),
                   Paragraph("PAID BACK", S_HEAD_R), Paragraph("NET", S_HEAD_R)]]
        for who, (lent, back) in s["lending"][:10]:
            rows_t.append([
                Paragraph(_esc(who), S_CELL), Paragraph(inr2(lent), S_CELL_R),
                Paragraph(inr2(back), S_CELL_R), Paragraph(inr2(lent - back), S_CELL_R),
            ])
        story.append(KeepTogether([
            Paragraph("Lent, by person", S_H2),
            _table(rows_t, [CONTENT_W - 3 * 80, 80, 80, 80]),
            Spacer(1, 3),
            Paragraph("Only this date range counts. A positive net means they still owe you; a negative net means "
                      "they paid back more than you lent here (for example, an older loan).", S_NOTE),
        ]))

    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=18 * mm,
        title="Minto spending report", author="Minto",
    )
    frame = Frame(MARGIN, 18 * mm, CONTENT_W, PAGE_H - MARGIN - 18 * mm, id="main",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_footer)])
    doc.build(story)
    return buf.getvalue()
