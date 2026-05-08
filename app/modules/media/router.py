"""Shared media upload endpoint — uploads files to S3 and returns the public URL."""
import io
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User

router = APIRouter(tags=["Media"])

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


async def upload_to_s3(file: UploadFile, folder: str) -> dict:
    """Validate + upload a file to S3, return {url, key}. Caller is
    responsible for authorisation — this helper does no auth checks
    of its own. Used by both the authed /media/upload endpoint and
    the public onboarding-token-authed logo upload endpoint in
    clients/router.py."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"File type {file.content_type} not allowed. Use JPEG, PNG, WebP, or GIF.",
        )

    content = await file.read()
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=422, detail="File too large. Maximum size is 5 MB.")

    if not settings.aws_access_key_id or not settings.aws_s3_bucket_name:
        # Dev fallback — return a placeholder URL so the form flow is
        # testable without real S3 credentials.
        ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
        filename = f"{folder}/{uuid.uuid4()}.{ext}"
        return {"url": f"https://placeholder.rootstalk.in/{filename}", "key": filename}

    import boto3
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_s3_region,
    )
    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
    key = f"rootstalk/{folder}/{uuid.uuid4()}.{ext}"
    s3.put_object(
        Bucket=settings.aws_s3_bucket_name,
        Key=key,
        Body=content,
        ContentType=file.content_type,
        ACL="public-read",
    )
    url = f"https://{settings.aws_s3_bucket_name}.s3.{settings.aws_s3_region}.amazonaws.com/{key}"
    return {"url": url, "key": key}


@router.post("/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    folder: str = "media",
    current_user: User = Depends(get_current_user),
):
    """Authed upload — used by logged-in CA portal / SA portal flows.
    For the public CA onboarding flow (no auth token yet), use
    `/onboarding/{token}/logo-upload` in clients/router.py."""
    return await upload_to_s3(file, folder)
