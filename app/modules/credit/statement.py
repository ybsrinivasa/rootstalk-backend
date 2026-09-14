"""CMS v1.2 — dealer-signed statement of account PDF.

Reportlab-based server-side PDF generator. A4 portrait, printer-friendly.
Includes the opening-balance-carried-forward for the range's start date,
walks every CONFIRMED entry within the range in chronological order, and
prints a running balance in the rightmost column.

Not legally binding — the disclaimer at the bottom is explicit. This
is a convenience document for the dealer's own accounting + the
farmer's reference.

Copy is English-hardcoded; participates in the broader deferred backend
push i18n project (the same one that covers push notifications).
"""
import io
from datetime import date, datetime, timezone
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

from app.modules.credit.models import (
    CreditEntry, CreditEntryStatus, CreditEntryType, InitiatorParty,
)


_ENTRY_SIGN: dict[str, int] = {
    CreditEntryType.OPENING_BALANCE.value: +1,
    CreditEntryType.CREDIT_ADVANCED.value: +1,
    CreditEntryType.ADJUSTMENT_UP.value:   +1,
    CreditEntryType.PAYMENT_MADE.value:    -1,
    CreditEntryType.ADJUSTMENT_DOWN.value: -1,
    CreditEntryType.VOID.value:             0,
}

_TYPE_LABEL: dict[str, str] = {
    CreditEntryType.OPENING_BALANCE.value: "Opening balance",
    CreditEntryType.CREDIT_ADVANCED.value: "Credit",
    CreditEntryType.PAYMENT_MADE.value:    "Payment",
    CreditEntryType.ADJUSTMENT_UP.value:   "Adjustment (+)",
    CreditEntryType.ADJUSTMENT_DOWN.value: "Adjustment (-)",
    CreditEntryType.VOID.value:            "Void",
}


def _rupees(paise: int) -> str:
    """Indian-grouped rupee string like ₹1,23,456."""
    negative = paise < 0
    paise = abs(paise)
    rupees = paise // 100
    s = str(rupees)
    if len(s) <= 3:
        out = f"₹{s}"
    else:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = f"₹{','.join(parts)},{tail}"
    return f"−{out}" if negative else out


def _fmt_date(d: date) -> str:
    return d.strftime("%d %b %Y")


def render_statement_pdf(
    *,
    dealer_name: str,
    dealer_phone: Optional[str],
    shop_name: Optional[str],
    shop_address: Optional[str],
    farmer_name: str,
    farmer_phone: Optional[str],
    account_opened_at: datetime,
    all_confirmed_entries: list[CreditEntry],
    from_date: date,
    to_date: date,
) -> bytes:
    """Render a statement PDF and return the raw bytes.

    `all_confirmed_entries` must contain every CONFIRMED entry on the
    account (across all time), sorted or unsorted — this function
    sorts internally by entry_date + created_at to compute the
    opening balance carried forward. Only entries with entry_date in
    [from_date, to_date] appear in the table.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.2 * cm, bottomMargin=1.5 * cm,
        title=f"Statement — {farmer_name}",
    )

    styles = getSampleStyleSheet()
    style_title = ParagraphStyle('title', parent=styles['Title'],
        fontName='Helvetica-Bold', fontSize=16, alignment=TA_LEFT, spaceAfter=2)
    style_subtitle = ParagraphStyle('subtitle', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#666'), alignment=TA_LEFT)
    style_h2 = ParagraphStyle('h2', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=12, alignment=TA_LEFT, spaceAfter=2)
    style_meta = ParagraphStyle('meta', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#333'), alignment=TA_LEFT)
    style_meta_right = ParagraphStyle('metar', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#333'), alignment=TA_RIGHT)
    style_footer = ParagraphStyle('footer', parent=styles['Normal'],
        fontSize=8, textColor=colors.HexColor('#888'), alignment=TA_CENTER)
    style_disclaimer = ParagraphStyle('disclaimer', parent=styles['Normal'],
        fontSize=8, textColor=colors.HexColor('#666'), alignment=TA_LEFT,
        leading=11)

    story = []

    # ── Header: shop + statement label ───────────────────────────────
    header_data = [[
        [
            Paragraph(shop_name or dealer_name, style_title),
            Paragraph(shop_address or "", style_subtitle),
            Paragraph(f"Dealer: {dealer_name}", style_subtitle),
            Paragraph(f"Phone: {dealer_phone}" if dealer_phone else "", style_subtitle),
        ],
        [
            Paragraph("STATEMENT OF ACCOUNT", style_h2),
            Paragraph(f"{_fmt_date(from_date)} — {_fmt_date(to_date)}", style_meta_right),
            Paragraph(f"Generated {_fmt_date(date.today())}", style_meta_right),
        ],
    ]]
    header_table = Table(header_data, colWidths=[10 * cm, 7 * cm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.4 * cm))

    # Horizontal rule as a 1-row 1-col table.
    hr = Table([['']], colWidths=[17 * cm], rowHeights=[1])
    hr.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 0.75, colors.HexColor('#c8c8c8')),
    ]))
    story.append(hr)
    story.append(Spacer(1, 0.3 * cm))

    # ── Farmer info ──────────────────────────────────────────────────
    account_since = account_opened_at.astimezone(timezone.utc).date()
    farmer_data = [[
        Paragraph(f"<b>Farmer:</b> {farmer_name}", style_meta),
        Paragraph(f"<b>Phone:</b> {farmer_phone or '—'}", style_meta),
        Paragraph(f"<b>Account since:</b> {_fmt_date(account_since)}", style_meta),
    ]]
    farmer_table = Table(farmer_data, colWidths=[6 * cm, 5 * cm, 6 * cm])
    farmer_table.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(farmer_table)
    story.append(Spacer(1, 0.5 * cm))

    # ── Balance walk ─────────────────────────────────────────────────
    entries_sorted = sorted(
        all_confirmed_entries,
        key=lambda e: (e.entry_date, e.created_at),
    )
    # Opening balance = sum of signed entries with entry_date < from_date.
    opening_balance = sum(
        _ENTRY_SIGN.get(e.entry_type, 0) * e.amount_paise
        for e in entries_sorted
        if e.entry_date < from_date
    )
    in_range = [e for e in entries_sorted if from_date <= e.entry_date <= to_date]

    # Build the table.
    table_data: list[list] = [[
        Paragraph("<b>Date</b>", style_meta),
        Paragraph("<b>Type</b>", style_meta),
        Paragraph("<b>Notes</b>", style_meta),
        Paragraph("<b>Credit</b>", style_meta_right),
        Paragraph("<b>Payment</b>", style_meta_right),
        Paragraph("<b>Balance</b>", style_meta_right),
    ]]

    # Opening balance row (always present, even if 0 — anchors the walk).
    table_data.append([
        Paragraph(_fmt_date(from_date), style_meta),
        Paragraph("<i>Opening balance</i>", style_meta),
        Paragraph(f"Balance carried forward to {_fmt_date(from_date)}", style_meta),
        Paragraph("—", style_meta_right),
        Paragraph("—", style_meta_right),
        Paragraph(_rupees(opening_balance), style_meta_right),
    ])

    running_balance = opening_balance
    for entry in in_range:
        sign = _ENTRY_SIGN.get(entry.entry_type, 0)
        running_balance += sign * entry.amount_paise
        credit_str = _rupees(entry.amount_paise) if sign > 0 else "—"
        payment_str = _rupees(entry.amount_paise) if sign < 0 else "—"
        note_bits = []
        if entry.initiator_note:
            note_bits.append(entry.initiator_note)
        if entry.payment_method:
            note_bits.append(f"({entry.payment_method}{f' · {entry.payment_ref}' if entry.payment_ref else ''})")
        if entry.due_date and entry.entry_type == CreditEntryType.CREDIT_ADVANCED.value:
            note_bits.append(f"Settle by {_fmt_date(entry.due_date)}")
        table_data.append([
            Paragraph(_fmt_date(entry.entry_date), style_meta),
            Paragraph(_TYPE_LABEL.get(entry.entry_type, entry.entry_type), style_meta),
            Paragraph(" · ".join(note_bits) or "—", style_meta),
            Paragraph(credit_str, style_meta_right),
            Paragraph(payment_str, style_meta_right),
            Paragraph(_rupees(running_balance), style_meta_right),
        ])

    # Closing row.
    table_data.append([
        Paragraph(f"<b>{_fmt_date(to_date)}</b>", style_meta),
        Paragraph("<b>Closing balance</b>", style_meta),
        Paragraph("", style_meta),
        Paragraph("", style_meta_right),
        Paragraph("", style_meta_right),
        Paragraph(f"<b>{_rupees(running_balance)}</b>", style_meta_right),
    ])

    entries_table = Table(
        table_data,
        colWidths=[2.6 * cm, 2.6 * cm, 5.3 * cm, 2.1 * cm, 2.1 * cm, 2.3 * cm],
        repeatRows=1,
    )
    entries_table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, 0), 0.75, colors.HexColor('#333')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F5F0E8')),
        ('LINEBELOW', (0, 1), (-1, 1), 0.25, colors.HexColor('#c8c8c8')),
        ('LINEABOVE', (0, -1), (-1, -1), 0.75, colors.HexColor('#333')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F5F0E8')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(entries_table)

    story.append(Spacer(1, 0.7 * cm))

    # ── Signature ────────────────────────────────────────────────────
    sig_data = [[
        [
            Paragraph("_" * 30, style_meta),
            Paragraph(f"<b>{dealer_name}</b>", style_meta),
            Paragraph("Signed by dealer", style_subtitle),
        ],
        Paragraph(
            f"<i>Positive balance = farmer owes dealer.<br/>"
            f"Only mutually confirmed entries are shown.<br/>"
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.</i>",
            style_disclaimer,
        ),
    ]]
    sig_table = Table(sig_data, colWidths=[8 * cm, 9 * cm])
    sig_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(sig_table)

    story.append(Spacer(1, 0.4 * cm))

    # ── Disclaimer ───────────────────────────────────────────────────
    story.append(Paragraph(
        "<b>Disclaimer:</b> This statement is a system-generated summary "
        "of entries mutually confirmed by both parties on RootsTalk as of "
        "the generation date. It is not legally binding. Any pending, "
        "disputed, or voided entries are excluded from the running balance.",
        style_disclaimer,
    ))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _footer(canvas: Canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(colors.HexColor('#999'))
    canvas.drawCentredString(
        A4[0] / 2, 0.8 * cm,
        f"Generated by RootsTalk · Page {doc.page}",
    )
    canvas.restoreState()
