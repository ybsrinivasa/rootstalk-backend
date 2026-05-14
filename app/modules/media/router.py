"""Shared media upload endpoint — uploads files to S3 and returns the public URL."""
import io
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User

router = APIRouter(tags=["Media"])

# Batch 36 (2026-05-14): widened from image-only to image + audio so
# advisory authoring can attach voice notes. Video stays out of this
# endpoint — SEs paste a hyperlink for video content instead.
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
AUDIO_CONTENT_TYPES = {
    "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/aac",
    "audio/ogg", "audio/opus",
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/webm",
}
ALLOWED_CONTENT_TYPES = IMAGE_CONTENT_TYPES | AUDIO_CONTENT_TYPES
# Audio voice-notes need more room than logos. 25 MB covers ~25 min
# at 128 kbps mp3 — enough for a long advisory tip.
MAX_SIZE_BYTES = 25 * 1024 * 1024


async def upload_to_s3(
    file: UploadFile,
    folder: str,
    *,
    allowed_types: set[str] | None = None,
    max_size_bytes: int | None = None,
) -> dict:
    """Validate + upload a file to S3, return {url, key}. Caller is
    responsible for authorisation — this helper does no auth checks
    of its own. Used by both the authed /media/upload endpoint and
    the public onboarding-token-authed logo upload endpoint in
    clients/router.py.

    `allowed_types` and `max_size_bytes` let callers narrow the
    content-type whitelist and size cap below the module-wide defaults
    (image+audio, 25 MB) — logo uploads scope down to image-only at
    5 MB, advisory authoring uses the module defaults."""
    types = allowed_types or ALLOWED_CONTENT_TYPES
    cap = max_size_bytes or MAX_SIZE_BYTES
    if file.content_type not in types:
        # Build a human-readable hint from the active whitelist.
        if types == IMAGE_CONTENT_TYPES:
            hint = "Use JPEG, PNG, WebP, or GIF."
        elif types == AUDIO_CONTENT_TYPES:
            hint = "Use MP3, AAC, OGG, WAV, or WebM."
        else:
            hint = "Use JPEG/PNG/WebP/GIF for images or MP3/AAC/OGG/WAV/WebM for audio."
        raise HTTPException(
            status_code=422,
            detail=f"File type {file.content_type} not allowed. {hint}",
        )

    content = await file.read()
    if len(content) > cap:
        mb = cap // (1024 * 1024)
        raise HTTPException(status_code=422, detail=f"File too large. Maximum size is {mb} MB.")

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
