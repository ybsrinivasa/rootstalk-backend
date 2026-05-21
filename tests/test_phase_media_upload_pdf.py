"""PDF acceptance in /media/upload — regression test.

Dealer Shop Registration Certificates can be PDFs (passports,
certificates, scans). Before commit f56b675-followup the backend's
ALLOWED_CONTENT_TYPES was image + audio only and rejected PDFs
with 422; the PWA's silent error-swallow made it look like the
button was broken.
"""
from __future__ import annotations

import io
import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.modules.media.router import (
    ALLOWED_CONTENT_TYPES,
    DOCUMENT_CONTENT_TYPES,
    upload_to_s3,
)
from tests.conftest import requires_docker


def test_pdf_is_in_allowed_content_types():
    assert "application/pdf" in DOCUMENT_CONTENT_TYPES
    assert "application/pdf" in ALLOWED_CONTENT_TYPES


@requires_docker
@pytest.mark.asyncio
async def test_upload_to_s3_accepts_pdf():
    """Dev-fallback path returns a placeholder URL when no S3 creds
    are configured; the test environment doesn't have AWS creds so
    we exercise validation + the placeholder code path."""
    pdf = UploadFile(
        filename="cert.pdf",
        file=io.BytesIO(b"%PDF-1.4 test content"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await upload_to_s3(pdf, folder="dealer-docs")
    assert out["url"].endswith(".pdf")


@requires_docker
@pytest.mark.asyncio
async def test_upload_to_s3_rejects_unknown_type():
    """Sanity — the whitelist still excludes random binary types."""
    bin_file = UploadFile(
        filename="evil.exe",
        file=io.BytesIO(b"MZ\x90\x00"),
        headers=Headers({"content-type": "application/x-msdownload"}),
    )
    with pytest.raises(HTTPException) as exc:
        await upload_to_s3(bin_file, folder="dealer-docs")
    assert exc.value.status_code == 422
