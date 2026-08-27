"""Business-intelligence / reporting engine for the admin platform.

Produces professional, insurer-ready dashboards for four domains — Providers,
Stores, Marketplace and Consultants — from the unmanaged mirror models that point
at the live backend tables. Each dashboard is a plain data structure (KPIs, time
series, tables) so the SAME computation drives the on-screen dashboard, the CSV
data extract and the PDF report.

Design notes:
  * Every domain builder is wrapped so a missing/empty table degrades to an empty
    section with a note rather than crashing a report.
  * Amounts are GBP. Period metrics use created_at within the selected range;
    "snapshot" KPIs (e.g. active stores) are as-of-now.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import Avg, Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from .. import models as m

logger = logging.getLogger(__name__)

CURRENCY = "£"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class KPI:
    label: str
    value: str          # display string (formatted)
    raw: float = 0.0    # numeric, for CSV
    hint: str = ""


@dataclass
class Series:
    title: str
    labels: list          # x-axis labels
    values: list          # numeric values
    kind: str = "bar"     # 'bar' | 'line'
    money: bool = False


@dataclass
class Table:
    title: str
    columns: list
    rows: list


@dataclass
class Dashboard:
    key: str
    title: str
    subtitle: str = ""
    kpis: list = field(default_factory=list)
    series: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def default_range():
    """Default reporting window: the last 12 whole months up to today."""
    end = timezone.now()
    start = (end - timedelta(days=365))
    return start, end


def parse_range(start_str, end_str):
    start, end = default_range()
    try:
        if start_str:
            start = timezone.make_aware(datetime.strptime(start_str, "%Y-%m-%d"))
    except (ValueError, TypeError):
        pass
    try:
        if end_str:
            end = timezone.make_aware(
                datetime.strptime(end_str, "%Y-%m-%d")
            ) + timedelta(days=1)  # inclusive of the end day
    except (ValueError, TypeError):
        pass
    return start, end


def money(value) -> str:
    try:
        return f"{CURRENCY}{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return f"{CURRENCY}0.00"


def num(value) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def pct(part, whole) -> str:
    try:
        whole = float(whole or 0)
        if whole <= 0:
            return "0%"
        return f"{(float(part or 0) / whole) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0%"


def _month_labels(start, end):
    """List of (year, month, 'Mon YY') buckets spanning the range."""
    out = []
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cur <= last:
        out.append((cur.year, cur.month, cur.strftime("%b %y")))
        cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return out


def _monthly_series(qs, date_field, start, end, *, title, value_expr=None, money_flag=False):
    """Build a zero-filled monthly Series from a queryset."""
    buckets = _month_labels(start, end)
    key = {(y, mth): 0 for (y, mth, _lbl) in buckets}
    try:
        rows = (
            qs.filter(**{f"{date_field}__range": (start, end)})
            .annotate(_month=TruncMonth(date_field))
            .values("_month")
            .annotate(_v=(Sum(value_expr) if value_expr else Count("id")))
            .order_by("_month")
        )
        for r in rows:
            mth = r["_month"]
            if mth:
                key[(mth.year, mth.month)] = float(r["_v"] or 0)
    except Exception:
        logger.exception("monthly series failed for %s", title)
    labels = [lbl for (_y, _m, lbl) in buckets]
    values = [key.get((y, mn), 0) for (y, mn, _l) in buckets]
    return Series(title=title, labels=labels, values=values, kind="line" if money_flag else "bar", money=money_flag)


def _status_rows(qs, field_name, *, top=12):
    """Return [[status, count], ...] grouped by a status-like field."""
    try:
        rows = (
            qs.values(field_name)
            .annotate(c=Count("id"))
            .order_by("-c")[:top]
        )
        return [[(r.get(field_name) or "—"), r["c"]] for r in rows]
    except Exception:
        logger.exception("status rows failed for %s", field_name)
        return []


def _sum(qs, expr):
    try:
        return qs.aggregate(
            t=Coalesce(Sum(expr), 0, output_field=DecimalField(max_digits=16, decimal_places=2))
        )["t"] or 0
    except Exception:
        logger.exception("sum failed for %s", expr)
        return 0


def _count(qs):
    try:
        return qs.count()
    except Exception:
        logger.exception("count failed")
        return 0


DELETED_SUFFIX = "@deleted.invalid"


# ---------------------------------------------------------------------------
# Domain builders
# ---------------------------------------------------------------------------

def providers_dashboard(start, end) -> Dashboard:
    d = Dashboard(key="providers", title="Providers & Accounts",
                  subtitle="Registrations, verification and risk posture across all account types.")
    try:
        users = m.ExternalUser.objects.all()
        period = users.filter(created_at__range=(start, end))
        breeders = m.ExternalBreederProfile.objects.all()
        consultants = m.ExternalConsultantProfile.objects.all()

        total = _count(users)
        verified = _count(users.filter(is_verified=True))
        at_risk = _count(users.filter(is_at_risk=True))
        deleted = _count(users.filter(email__iendswith=DELETED_SUFFIX))

        d.kpis = [
            KPI("Total accounts", num(total), total),
            KPI("New accounts (period)", num(_count(period)), _count(period)),
            KPI("Breeder profiles", num(_count(breeders)), _count(breeders)),
            KPI("Consultant profiles", num(_count(consultants)), _count(consultants)),
            KPI("Verified accounts", pct(verified, total), verified, hint=f"{num(verified)} verified"),
            KPI("At-risk accounts", num(at_risk), at_risk, hint="Flagged by trust engine"),
            KPI("Erased (GDPR)", num(deleted), deleted),
        ]
        d.series = [
            _monthly_series(users, "created_at", start, end, title="New account registrations"),
        ]
        d.tables = [
            Table("Consultant approval status", ["Status", "Count"],
                  _status_rows(consultants, "admin_status")),
            Table("Regulatory tier distribution", ["Tier", "Accounts"],
                  _status_rows(users, "current_regulatory_tier")),
        ]
    except Exception as exc:
        logger.exception("providers dashboard failed")
        d.note = f"Some provider metrics could not be computed: {exc}"
    return d


def stores_dashboard(start, end) -> Dashboard:
    d = Dashboard(key="stores", title="Stores (Marketplace Sellers)",
                  subtitle="Seller onboarding, payout readiness and licence/verification coverage.")
    try:
        sellers = m.ExternalMarketplaceSellerProfile.objects.all()
        verifs = m.ExternalBreederVerification.objects.all()

        total = _count(sellers)
        payouts = _count(sellers.filter(payouts_enabled=True))
        delivery = _count(sellers.filter(delivery_sales_enabled=True))
        suspended = _count(sellers.filter(delivery_suspended=True))
        approved_lic = _count(verifs.filter(status__iexact="approved"))
        today = timezone.now().date()
        expiring = _count(
            verifs.filter(status__iexact="approved",
                          expiry_date__isnull=False,
                          expiry_date__range=(today, today + timedelta(days=60)))
        )
        expired = _count(
            verifs.filter(status__iexact="approved", expiry_date__isnull=False,
                          expiry_date__lt=today)
        )

        d.kpis = [
            KPI("Total stores", num(total), total),
            KPI("Payouts enabled", pct(payouts, total), payouts, hint=f"{num(payouts)} stores"),
            KPI("Delivery enabled", num(delivery), delivery),
            KPI("Suspended (delivery)", num(suspended), suspended),
            KPI("Approved licences", num(approved_lic), approved_lic),
            KPI("Licences expiring ≤60d", num(expiring), expiring, hint="Insurance risk"),
            KPI("Licences expired", num(expired), expired, hint="Insurance risk"),
        ]
        d.series = [
            _monthly_series(verifs, "created_at", start, end, title="Licence verifications submitted"),
        ]
        d.tables = [
            Table("Stripe Connect status", ["Status", "Stores"],
                  _status_rows(sellers, "stripe_connect_status")),
            Table("Licence verification status", ["Status", "Count"],
                  _status_rows(verifs, "status")),
        ]
    except Exception as exc:
        logger.exception("stores dashboard failed")
        d.note = f"Some store metrics could not be computed: {exc}"
    return d


def marketplace_dashboard(start, end) -> Dashboard:
    d = Dashboard(key="marketplace", title="Marketplace",
                  subtitle="Order value, settlement, disputes, refunds and payment-failure exposure.")
    try:
        res = m.ExternalBreederReservation.objects.all()
        period = res.filter(created_at__range=(start, end))
        disputes = m.ExternalReservationDispute.objects.filter(opened_at__range=(start, end))
        refunds = m.ExternalRefund.objects.filter(created_at__range=(start, end))
        payfail = m.ExternalPaymentFailureLog.objects.filter(created_at__range=(start, end))

        orders = _count(period)
        paid = period.filter(payment_status__iexact="paid")
        gmv = _sum(period, "total_amount")
        platform_rev = _sum(period, "platform_fee")
        completed = _count(period.filter(status__iexact="completed"))
        dispute_ct = _count(disputes)
        refund_ct = _count(refunds)
        refund_val = _sum(refunds, "amount")
        payfail_ct = _count(payfail)
        payfail_val = _sum(payfail, "amount")

        d.kpis = [
            KPI("Orders (period)", num(orders), orders),
            KPI("Gross order value", money(gmv), float(gmv)),
            KPI("Platform revenue", money(platform_rev), float(platform_rev)),
            KPI("Completed orders", pct(completed, orders), completed),
            KPI("Disputes (period)", num(dispute_ct), dispute_ct, hint=pct(dispute_ct, orders) + " of orders"),
            KPI("Refunds (period)", f"{num(refund_ct)} · {money(refund_val)}", refund_ct),
            KPI("Payment failures", f"{num(payfail_ct)} · {money(payfail_val)}", payfail_ct, hint="Insurance risk"),
        ]
        d.series = [
            _monthly_series(res, "created_at", start, end, title="Gross order value (£)",
                            value_expr="total_amount", money_flag=True),
            _monthly_series(res, "created_at", start, end, title="Order volume"),
        ]
        d.tables = [
            Table("Order status breakdown", ["Status", "Orders"], _status_rows(period, "status")),
            Table("Dispute reasons", ["Reason", "Count"], _status_rows(disputes, "reason")),
            Table("Dispute resolutions", ["Resolution", "Count"], _status_rows(disputes, "resolution")),
            Table("Refund reasons", ["Reason", "Count"], _status_rows(refunds, "reason")),
        ]
    except Exception as exc:
        logger.exception("marketplace dashboard failed")
        d.note = f"Some marketplace metrics could not be computed: {exc}"
    return d


def consultants_dashboard(start, end) -> Dashboard:
    d = Dashboard(key="consultants", title="Consultants",
                  subtitle="Booking volume, consultation revenue, delivery quality and warnings.")
    try:
        bookings = m.ExternalConsultantBooking.objects.all()
        period = bookings.filter(created_at__range=(start, end))
        warnings = m.ExternalConsultantWarning.objects.filter(created_at__range=(start, end))

        total = _count(period)
        revenue = _sum(period, "full_price")
        fees = _sum(period, "booking_fee")
        completed = _count(period.filter(status__iexact="completed"))
        cancelled = _count(period.filter(status__iexact="cancelled"))
        successful = _count(period.filter(was_successful=True))
        try:
            avg_rating = period.filter(rating__isnull=False).aggregate(a=Avg("rating"))["a"] or 0
        except Exception:
            avg_rating = 0

        d.kpis = [
            KPI("Bookings (period)", num(total), total),
            KPI("Consultation value", money(revenue), float(revenue)),
            KPI("Booking-fee revenue", money(fees), float(fees)),
            KPI("Completed", pct(completed, total), completed),
            KPI("Cancelled", num(cancelled), cancelled, hint=pct(cancelled, total) + " of bookings"),
            KPI("Avg rating", f"{float(avg_rating):.2f}", float(avg_rating)),
            KPI("Warnings issued", num(_count(warnings)), _count(warnings), hint="Insurance risk"),
        ]
        d.series = [
            _monthly_series(bookings, "created_at", start, end, title="Consultation value (£)",
                            value_expr="full_price", money_flag=True),
            _monthly_series(bookings, "created_at", start, end, title="Booking volume"),
        ]
        d.tables = [
            Table("Booking status breakdown", ["Status", "Bookings"], _status_rows(period, "status")),
            Table("Payment status", ["Status", "Bookings"], _status_rows(period, "payment_status")),
            Table("Warning severity", ["Severity", "Count"], _status_rows(warnings, "severity")),
        ]
    except Exception as exc:
        logger.exception("consultants dashboard failed")
        d.note = f"Some consultant metrics could not be computed: {exc}"
    return d


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY = {
    "providers": ("Providers & Accounts", providers_dashboard),
    "stores": ("Stores", stores_dashboard),
    "marketplace": ("Marketplace", marketplace_dashboard),
    "consultants": ("Consultants", consultants_dashboard),
}
ORDER = ["providers", "stores", "marketplace", "consultants"]


def build_dashboard(key, start, end) -> Dashboard | None:
    entry = REGISTRY.get(key)
    if not entry:
        return None
    return entry[1](start, end)


def build_many(keys, start, end):
    return [d for d in (build_dashboard(k, start, end) for k in keys) if d]


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def to_csv(dashboards, start, end) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Aqua AI — BI Report"])
    w.writerow(["Period", start.strftime("%Y-%m-%d"), "to", (end - timedelta(days=1)).strftime("%Y-%m-%d")])
    w.writerow(["Generated", timezone.now().strftime("%Y-%m-%d %H:%M UTC")])
    for d in dashboards:
        w.writerow([])
        w.writerow([f"# {d.title}"])
        if d.subtitle:
            w.writerow([d.subtitle])
        if d.note:
            w.writerow(["Note", d.note])
        w.writerow([])
        w.writerow(["Metric", "Value", "Detail"])
        for k in d.kpis:
            w.writerow([k.label, k.raw, k.hint or k.value])
        for s in d.series:
            w.writerow([])
            w.writerow([s.title])
            w.writerow(s.labels)
            w.writerow(s.values)
        for t in d.tables:
            w.writerow([])
            w.writerow([t.title])
            w.writerow(t.columns)
            for row in t.rows:
                w.writerow(row)
    return buf.getvalue()


def to_pdf(dashboards, start, end) -> bytes:
    """Professional, insurer-ready PDF: branded header/footer, KPI cards, and
    native charts (line trends + pie breakdowns) via reportlab.graphics."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether,
        Table as RLTable, TableStyle,
    )
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.charts.piecharts import Pie

    from .charts import PALETTE

    PAL = [colors.HexColor(c) for c in PALETTE]
    NAVY = colors.HexColor("#1E3A8A")
    BLUE = colors.HexColor("#3B82F6")
    INK = colors.HexColor("#0F172A")
    SLATE = colors.HexColor("#475569")
    MUTE = colors.HexColor("#94A3B8")
    HAIR = colors.HexColor("#E2E8F0")
    CARD = colors.HexColor("#F8FAFC")

    ss = getSampleStyleSheet()
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=15, spaceBefore=6, spaceAfter=2, textColor=NAVY)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontSize=10.5, spaceBefore=10, spaceAfter=4, textColor=INK)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9, textColor=SLATE, leading=12)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=MUTE, leading=11)
    kpi_v = ParagraphStyle("kpi_v", parent=ss["Normal"], fontSize=15, textColor=INK, leading=17)
    kpi_l = ParagraphStyle("kpi_l", parent=ss["Normal"], fontSize=7.5, textColor=SLATE, leading=9)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title="Aqua AI — BI Report",
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=26 * mm, bottomMargin=16 * mm,
    )
    content_w = doc.width
    period = f"{start.strftime('%d %b %Y')} – {(end - timedelta(days=1)).strftime('%d %b %Y')}"
    gen = timezone.now().strftime("%d %b %Y %H:%M UTC")

    def _decorate(canvas, dc):
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, A4[1] - 18 * mm, A4[0], 18 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawString(16 * mm, A4[1] - 12 * mm, "Aqua AI — Business Intelligence Report")
        canvas.setFillColor(colors.HexColor("#C7D2FE"))
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(A4[0] - 16 * mm, A4[1] - 12 * mm, period)
        canvas.setStrokeColor(HAIR)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, 14 * mm, A4[0] - 16 * mm, 14 * mm)
        canvas.setFillColor(MUTE)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(16 * mm, 10 * mm, f"Confidential · Generated {gen}")
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Page {dc.page}")
        canvas.restoreState()

    def kpi_cards(kpis):
        cells = []
        for k in kpis:
            inner = RLTable(
                [[Paragraph(f"<b>{k.value}</b>", kpi_v)],
                 [Paragraph(k.label, kpi_l)]]
                + ([[Paragraph(k.hint, small)]] if k.hint else []),
                colWidths=[content_w / 3.0 - 6],
            )
            inner.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), CARD),
                ("LINEABOVE", (0, 0), (-1, 0), 2, BLUE),
                ("BOX", (0, 0), (-1, -1), 0.5, HAIR),
                ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                ("TOPPADDING", (0, 1), (-1, -1), 1),
            ]))
            cells.append(inner)
        while len(cells) % 3:
            cells.append("")
        rows = [cells[i:i + 3] for i in range(0, len(cells), 3)]
        t = RLTable(rows, colWidths=[content_w / 3.0] * 3, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    def line_chart_flow(series):
        try:
            dw = Drawing(content_w, 130)
            lc = HorizontalLineChart()
            lc.x = 34
            lc.y = 20
            lc.width = content_w - 48
            lc.height = 95
            lc.data = [[float(v or 0) for v in series.values] or [0]]
            lc.categoryAxis.categoryNames = [str(l) for l in series.labels]
            lc.categoryAxis.labels.fontSize = 6
            lc.categoryAxis.labels.angle = 30
            lc.categoryAxis.labels.dy = -4
            lc.categoryAxis.labels.boxAnchor = "ne"
            lc.valueAxis.valueMin = 0
            lc.valueAxis.labels.fontSize = 6.5
            lc.valueAxis.strokeColor = HAIR
            lc.lines[0].strokeColor = BLUE
            lc.lines[0].strokeWidth = 2
            lc.fillColor = None
            dw.add(lc)
            return dw
        except Exception:
            logger.exception("pdf line chart failed")
            return Paragraph("(chart unavailable)", small)

    def pie_block(table):
        data = [(str(l), float(v or 0)) for l, v in table.rows if float(v or 0) > 0]
        if not data:
            return Paragraph("No data in this period.", small)
        try:
            dw = Drawing(150, 150)
            pie = Pie()
            pie.x, pie.y, pie.width, pie.height = 20, 18, 112, 112
            pie.data = [v for _l, v in data]
            pie.simpleLabels = 1
            pie.slices.label_visible = 0
            for i in range(len(data)):
                pie.slices[i].fillColor = PAL[i % len(PAL)]
                pie.slices[i].strokeColor = colors.white
                pie.slices[i].strokeWidth = 0.75
            dw.add(pie)
        except Exception:
            logger.exception("pdf pie failed")
            dw = Paragraph("(chart unavailable)", small)

        total = sum(v for _l, v in data) or 1
        leg_rows = []
        leg_style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                     ("LEFTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
                     ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        for i, (label, value) in enumerate(data):
            leg_rows.append([
                "", Paragraph(label.title(), body),
                Paragraph(f"<b>{int(value)}</b> · {value / total * 100:.0f}%", body),
            ])
            leg_style.append(("BACKGROUND", (0, i), (0, i), PAL[i % len(PAL)]))
        legend = RLTable(leg_rows, colWidths=[9, content_w - 150 - 90, 78])
        legend.setStyle(TableStyle(leg_style))
        block = RLTable([[dw, legend]], colWidths=[150, content_w - 150])
        block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        return block

    story = []
    for di, d in enumerate(dashboards):
        if di:
            story.append(PageBreak())
        head = [Paragraph(d.title, h2)]
        if d.subtitle:
            head.append(Paragraph(d.subtitle, body))
        if d.note:
            head.append(Paragraph(d.note, small))
        head.append(Spacer(1, 6))
        head.append(kpi_cards(d.kpis))
        story.append(KeepTogether(head))

        for s in d.series:
            story.append(Paragraph(s.title, h3))
            story.append(line_chart_flow(s))

        for t in d.tables:
            if not t.rows:
                continue
            story.append(KeepTogether([Paragraph(t.title, h3), pie_block(t)]))

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
