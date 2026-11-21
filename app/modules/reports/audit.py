"""CSV download audit — generic sink for every CSV export endpoint.

Any CSV endpoint (CP or SA) calls `record_csv_download(...)` after the
response payload is built. Ingest-only here; the SA Portal read side
lives in `app/modules/reports/sa_router.py` under
`/admin/reports/csv-downloads`.

Deliberately non-fatal: if the audit write fails, the export still
succeeds — we log the error and move on. Losing the actual export
because the audit log hiccuped would be worse than a missing audit
row for compliance review.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.platform.models import User
from app.modules.reports.models import ReportCsvDownload

logger = logging.getLogger(__name__)


async def record_csv_download(
    db: AsyncSession,
    *,
    user: User,
    report_name: str,
    client_id: Optional[str],
    filters: Optional[dict[str, Any]] = None,
    row_count: Optional[int] = None,
    request: Optional[Request] = None,
) -> None:
    """Insert one audit row. Non-fatal: swallows + logs on failure.

    `report_name` is a stable short identifier (e.g. ``"queries_open"``,
    ``"queries_expired"``, ``"queries_responded"``). Future reports pick
    their own.

    `filters` captures whatever the endpoint used to produce the export
    — crop_id, state, district, period_from/to, status, pundit ids, etc.
    Keep it the same shape you pass to the endpoint; SA viewer renders
    it as JSON.

    `request` is optional — when passed, we store the caller IP + UA for
    compliance review.
    """
    try:
        ip: Optional[str] = None
        ua: Optional[str] = None
        if request is not None:
            # Honour X-Forwarded-For when behind nginx; fall back to the
            # socket peer. Trimmed to 64 chars for the column width.
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                ip = fwd.split(",")[0].strip()[:64]
            elif request.client is not None:
                ip = request.client.host[:64]
            ua_raw = request.headers.get("user-agent")
            if ua_raw:
                ua = ua_raw[:255]
        row = ReportCsvDownload(
            user_id=user.id,
            client_id=client_id,
            report_name=report_name,
            filters_json=filters,
            row_count=row_count,
            ip_address=ip,
            user_agent=ua,
        )
        db.add(row)
        await db.commit()
    except Exception as e:
        # Belt-and-braces: never break the export because the audit
        # write failed. Compliance would rather have a missing row
        # than a missing export.
        logger.exception("record_csv_download failed: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass
