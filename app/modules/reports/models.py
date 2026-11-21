import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Integer, DateTime, ForeignKey, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _new_uuid():
    return str(uuid.uuid4())


# ── CSV download audit (2026-11-21) ────────────────────────────────────────
#
# Every CSV export from the CA Portal (and SA Portal, in future) writes a
# row here. Generic shape — `report_name` identifies which report, and
# `filters_json` captures the filter set used. Any future audited export
# simply calls `record_csv_download(...)` with its own `report_name`; no
# migration or schema change needed per-report.
#
# Read surface is in the SA Portal only (`/reports/csv-downloads`), SA-
# gated via the existing admin auth stack. No automatic retention — this
# is compliance evidence, keep it until legal says otherwise.

class ReportCsvDownload(Base):
    __tablename__ = "report_csv_downloads"
    __table_args__ = (
        Index("ix_report_csv_downloads_downloaded_at", "downloaded_at"),
        Index("ix_report_csv_downloads_client_id", "client_id"),
        Index("ix_report_csv_downloads_report_name", "report_name"),
        Index("ix_report_csv_downloads_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    client_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True,
    )
    report_name: Mapped[str] = mapped_column(String(80), nullable=False)
    filters_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
