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
    """Professional, insurer-ready PDF via reportlab (already a project dep)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table as RLTable, TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title="Aqua AI — BI Report",
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
    )
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20, spaceAfter=4, textColor=colors.HexColor("#0F172A"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=14, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1E3A8A"))
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#334155"))
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#475569"))
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#94A3B8"))

    story = []
    story.append(Paragraph("Aqua AI — Business Intelligence Report", h1))
    story.append(Paragraph(
        f"Reporting period: {start.strftime('%d %b %Y')} – {(end - timedelta(days=1)).strftime('%d %b %Y')}", body))
    story.append(Paragraph(f"Generated {timezone.now().strftime('%d %b %Y %H:%M UTC')} · Confidential", small))
    story.append(Spacer(1, 6))

    grid = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])

    for d in dashboards:
        story.append(Paragraph(d.title, h2))
        if d.subtitle:
            story.append(Paragraph(d.subtitle, body))
        if d.note:
            story.append(Paragraph(d.note, small))

        # KPI grid (3 columns)
        cells = [[Paragraph(f"<b>{k.value}</b><br/><font size=7 color='#64748B'>{k.label}</font>", body)] for k in d.kpis]
        rows = [cells[i:i + 3] for i in range(0, len(cells), 3)]
        rows = [r + [[Paragraph("", body)]] * (3 - len(r)) for r in rows]
        flat = [[c[0] for c in r] for r in rows]
        if flat:
            kt = RLTable(flat, colWidths=[doc.width / 3.0] * 3)
            kt.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(kt)

        for t in d.tables:
            if not t.rows:
                continue
            story.append(Paragraph(t.title, h3))
            data = [t.columns] + [[str(c) for c in row] for row in t.rows]
            rlt = RLTable(data, repeatRows=1)
            rlt.setStyle(grid)
            story.append(rlt)
        story.append(Spacer(1, 8))

    doc.build(story)
    return buf.getvalue()
