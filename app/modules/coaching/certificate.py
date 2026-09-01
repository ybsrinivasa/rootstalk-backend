"""Coaching Sandbox — digital certificate PDF generation.

Reportlab-based server-side PDF generator. Landscape A4, single page,
Neytiri-branded. Design decisions locked with user 2026-09-01:
  - Fixed template of aspects covered (not coach-editable per session)
  - Coach signature = typed name only (no image upload for v1)
  - Single static template (no per-client branding)
  - PDF-only delivery (attachment, no HTML body variant)
  - Verification URL at bottom, `/verify/<cert_number>` on the
    client-portal domain

Layout, top-to-bottom:
  - Neytiri seal/logo (top center) — falls back to text if no logo
    configured in settings
  - "Certificate of Completion" title
  - Subtitle: "rootsTALK Coaching Program"
  - Recipient: "This is to certify that <STUDENT NAME>"
  - Body sentence: has successfully completed... in the context of X
  - Aspects covered (fixed list, bulleted)
  - Session dates
  - Grade (large, coloured by grade)
  - Coach signature line: typed name + "Coach"
  - Certificate number + verification URL (footer, small)
"""
import io
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.pdfgen.canvas import Canvas


# Fixed aspects-covered list — every certificate carries the same
# five items per user's 2026-09-01 decision. Coach-editable per
# session is a v2 possibility if cohorts start diverging in scope.
ASPECTS_COVERED = [
    "Company Administration",
    "Content Authoring — Packages, CHA, and Q&A",
    "Team Onboarding & Management",
    "Reporting & Analytics",
    "Field Operations via PWA",
]

# Grade → colour for the certificate visual (matches the SA-portal
# grade pill palette so a coach's cert print matches their UI).
GRADE_COLOURS = {
    "SATISFACTORY": colors.HexColor("#2563EB"),  # blue
    "GOOD":         colors.HexColor("#059669"),  # emerald
    "EXCELLENT":    colors.HexColor("#7D4196"),  # purple
}


def render_certificate_pdf(
    *,
    student_name: str,
    reference_client_name: str,
    coach_name: str,
    session_started_at: datetime,
    session_closed_at: datetime,
    grade: str,
    certificate_number: str,
    verification_url: str,
) -> bytes:
    """Compose the certificate PDF in-memory, return the byte blob.
    Caller uploads to S3 and emails.

    All input arguments are required — a certificate without a coach
    name or grade wouldn't be valid, so we raise KeyError via
    dict-style access if the caller forgets a field. Type hints keep
    call sites honest at import time.
    """
    buf = io.BytesIO()
    page_w, page_h = landscape(A4)
    c = Canvas(buf, pagesize=landscape(A4))

    # ── Ornamental border ────────────────────────────────────────────
    outer_margin = 1.2 * cm
    inner_margin = 1.5 * cm
    c.setStrokeColor(colors.HexColor("#7D4196"))
    c.setLineWidth(3)
    c.rect(outer_margin, outer_margin,
           page_w - 2 * outer_margin, page_h - 2 * outer_margin)
    c.setLineWidth(0.5)
    c.rect(inner_margin, inner_margin,
           page_w - 2 * inner_margin, page_h - 2 * inner_margin)

    # ── Neytiri branding (top center) ─────────────────────────────────
    y = page_h - 2.5 * cm
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.HexColor("#0D1B2A"))
    c.drawCentredString(page_w / 2, y, "NEYTIRI EYWAFARM AGRITECH PRIVATE LIMITED")

    y -= 0.55 * cm
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawCentredString(page_w / 2, y, "rootsTALK.in")

    # ── Title ─────────────────────────────────────────────────────────
    y -= 1.6 * cm
    c.setFont("Helvetica-Bold", 34)
    c.setFillColor(colors.HexColor("#0D1B2A"))
    c.drawCentredString(page_w / 2, y, "Certificate of Completion")

    y -= 0.9 * cm
    c.setFont("Helvetica-Oblique", 14)
    c.setFillColor(colors.HexColor("#7D4196"))
    c.drawCentredString(page_w / 2, y, "rootsTALK Coaching Program")

    # ── Recipient ─────────────────────────────────────────────────────
    y -= 1.6 * cm
    c.setFont("Helvetica", 12)
    c.setFillColor(colors.HexColor("#333333"))
    c.drawCentredString(page_w / 2, y, "This is to certify that")

    y -= 0.9 * cm
    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(colors.HexColor("#0D1B2A"))
    c.drawCentredString(page_w / 2, y, student_name)

    # ── Body sentence ─────────────────────────────────────────────────
    y -= 1.0 * cm
    c.setFont("Helvetica", 12)
    c.setFillColor(colors.HexColor("#333333"))
    c.drawCentredString(
        page_w / 2, y,
        "has successfully completed the rootsTALK Coaching Program",
    )
    y -= 0.55 * cm
    c.drawCentredString(
        page_w / 2, y,
        f"in the context of {reference_client_name}.",
    )

    # ── Aspects covered ───────────────────────────────────────────────
    y -= 1.0 * cm
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.HexColor("#0D1B2A"))
    c.drawCentredString(page_w / 2, y, "Aspects covered")
    y -= 0.15 * cm
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#333333"))
    # Two columns to keep the section compact
    col_w = 9 * cm
    left_x = (page_w - col_w * 2 - 1 * cm) / 2
    right_x = left_x + col_w + 1 * cm
    left_items = ASPECTS_COVERED[:3]
    right_items = ASPECTS_COVERED[3:]
    for i, item in enumerate(left_items):
        c.drawString(left_x, y - 0.55 * cm * (i + 1), f"• {item}")
    for i, item in enumerate(right_items):
        c.drawString(right_x, y - 0.55 * cm * (i + 1), f"• {item}")
    y -= 0.55 * cm * max(len(left_items), len(right_items))

    # ── Session dates + grade (bottom row) ────────────────────────────
    y -= 1.0 * cm
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#666666"))
    date_line = (
        f"Coaching session: {session_started_at.strftime('%d %b %Y')} — "
        f"{session_closed_at.strftime('%d %b %Y')}"
    )
    c.drawCentredString(page_w / 2, y, date_line)

    y -= 0.9 * cm
    c.setFont("Helvetica-Bold", 16)
    grade_colour = GRADE_COLOURS.get(grade, colors.HexColor("#333333"))
    c.setFillColor(grade_colour)
    grade_labels = {
        "SATISFACTORY": "Satisfactory",
        "GOOD": "Good",
        "EXCELLENT": "Excellent",
    }
    grade_label = grade_labels.get(grade, grade)
    c.drawCentredString(page_w / 2, y, f"Grade: {grade_label}")

    # ── Coach signature (bottom, right of center) ─────────────────────
    sig_y = 3.5 * cm
    sig_x = page_w - 8 * cm
    c.setStrokeColor(colors.HexColor("#0D1B2A"))
    c.setLineWidth(0.6)
    c.line(sig_x, sig_y + 0.5 * cm, sig_x + 6 * cm, sig_y + 0.5 * cm)
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.HexColor("#0D1B2A"))
    c.drawString(sig_x, sig_y, coach_name)
    c.setFont("Helvetica-Oblique", 9)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(sig_x, sig_y - 0.4 * cm, "Coach")

    # ── Certificate number + verify URL (footer) ──────────────────────
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#999999"))
    footer_y = 1.7 * cm
    c.drawString(
        inner_margin + 0.2 * cm, footer_y,
        f"Certificate No: {certificate_number}",
    )
    c.drawRightString(
        page_w - inner_margin - 0.2 * cm, footer_y,
        f"Verify: {verification_url}",
    )

    c.showPage()
    c.save()
    return buf.getvalue()
