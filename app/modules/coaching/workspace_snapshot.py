"""Coaching Sandbox — workspace snapshot PDF generator (2026-10-16).

At certification time we render a snapshot PDF of the student's
authored content (Packages, Timelines, Practices) and email it
alongside the certificate. This preserves a point-in-time record of
what the student built so they can look back after Cosh/upstream
content drifts.

v1 scope: a summary PDF listing Packages the student authored in
their coaching workspace, with Timeline and Practice counts per
Package. Full per-practice content detail can land in a later rev.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen.canvas import Canvas


def render_workspace_snapshot_pdf(
    *,
    student_name: str,
    workspace_name: str,
    reference_client_name: str,
    coach_name: str,
    session_started_at: datetime,
    session_closed_at: datetime,
    packages: list,  # list of (name, timeline_count, practice_count)
) -> bytes:
    """Produce a one-page summary PDF. Lightweight — this is a
    student archive, not a Cosh-grade spec. Caller uploads to S3 and
    attaches to the certification email."""
    buf = io.BytesIO()
    page_w, page_h = A4
    c = Canvas(buf, pagesize=A4)

    margin = 2 * cm

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.setFillColor(colors.HexColor("#1A5C2A"))
    c.drawString(margin, page_h - margin - 0.5 * cm,
                 "Workspace Snapshot")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#4B5563"))
    c.drawString(margin, page_h - margin - 1.2 * cm,
                 "Point-in-time record of your coaching workspace")

    # Student + session metadata
    y = page_h - margin - 2.5 * cm
    c.setFont("Helvetica", 11)
    c.setFillColor(colors.HexColor("#111827"))

    def _label_value(label: str, value: str, y_pos: float) -> float:
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#6B7280"))
        c.drawString(margin, y_pos, label)
        c.setFont("Helvetica", 11)
        c.setFillColor(colors.HexColor("#111827"))
        c.drawString(margin, y_pos - 0.5 * cm, value)
        return y_pos - 1.3 * cm

    y = _label_value("Student", student_name, y)
    y = _label_value("Workspace", workspace_name, y)
    y = _label_value("Reference client", reference_client_name, y)
    y = _label_value("Coach", coach_name, y)
    y = _label_value(
        "Session window",
        f"{session_started_at.strftime('%d %b %Y')} → {session_closed_at.strftime('%d %b %Y')}",
        y,
    )
    # Snapshot timestamp
    y = _label_value(
        "Snapshot generated",
        datetime.utcnow().strftime("%d %b %Y %H:%M UTC"),
        y,
    )

    # Separator
    y -= 0.5 * cm
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.setLineWidth(0.5)
    c.line(margin, y, page_w - margin, y)
    y -= 1 * cm

    # Packages section
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(colors.HexColor("#111827"))
    c.drawString(margin, y, f"Packages authored ({len(packages)})")
    y -= 0.8 * cm

    if not packages:
        c.setFont("Helvetica-Oblique", 10)
        c.setFillColor(colors.HexColor("#9CA3AF"))
        c.drawString(margin, y, "No packages were authored in this workspace.")
    else:
        c.setFont("Helvetica", 10)
        for name, tl_count, prac_count in packages:
            if y < margin + 2 * cm:
                c.showPage()
                y = page_h - margin
            c.setFillColor(colors.HexColor("#111827"))
            c.drawString(margin, y, f"• {name}")
            c.setFillColor(colors.HexColor("#6B7280"))
            c.setFont("Helvetica", 9)
            c.drawString(margin + 0.5 * cm, y - 0.45 * cm,
                         f"{tl_count} timeline(s) · {prac_count} practice(s)")
            c.setFont("Helvetica", 10)
            y -= 1.1 * cm

    # Footer
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#9CA3AF"))
    c.drawString(
        margin, margin,
        "This is a point-in-time snapshot of your coaching workspace. "
        "Upstream library updates after this date do not affect it.",
    )

    c.save()
    buf.seek(0)
    return buf.getvalue()
