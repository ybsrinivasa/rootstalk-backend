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
    # 2026-05-27: Android voice recorder reports `audio/x-m4a` (and
    # some browsers send `audio/m4a` / `audio/x-mp4`); iOS reports
    # `audio/mp4`. All four are the same underlying container — accept
    # every spelling so the farmer's m4a voice note doesn't bounce.
    "audio/mp4", "audio/x-m4a", "audio/m4a", "audio/x-mp4",
    "audio/aac",
    "audio/ogg", "audio/opus",
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/webm",
    # Older Android devices fall back to 3GPP audio in some recorder
    # apps.
    "audio/3gpp", "audio/amr",
    "audio/flac",
}
# 2026-05-27: video provisioned but the PWA doesn't ship the picker
# in V1 — the back-end column QueryMedia.media_type accepts "VIDEO"
# and the upload endpoint will accept these MIME types once the
# front-end exposes a Video button. Keeping the set narrow to common
# capture formats; widen on demand.
VIDEO_CONTENT_TYPES = {
    "video/mp4", "video/quicktime",
    "video/webm", "video/x-matroska",
    "video/3gpp",
}
# 2026-05-21: dealer shop-registration certs are commonly PDFs.
# Added at module scope so any document upload (not just the cert)
# can include PDFs without each caller having to widen the set.
DOCUMENT_CONTENT_TYPES = {"application/pdf"}
ALLOWED_CONTENT_TYPES = (
    IMAGE_CONTENT_TYPES | AUDIO_CONTENT_TYPES
    | VIDEO_CONTENT_TYPES | DOCUMENT_CONTENT_TYPES
)
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
    # Browsers can attach codec params ("audio/webm;codecs=opus",
    # "audio/mp4;codecs=mp4a.40.2") to the Content-Type on media
    # captured via MediaRecorder. Match on the bare MIME type — the
    # codec parameter is descriptive, not restrictive, for our
    # whitelist purposes.
    base_ct = (file.content_type or "").split(";", 1)[0].strip()
    if base_ct not in types:
        # Build a human-readable hint from the active whitelist.
        if types == IMAGE_CONTENT_TYPES:
            hint = "Use JPEG, PNG, WebP, or GIF."
        elif types == AUDIO_CONTENT_TYPES:
            hint = "Use MP3, M4A, AAC, OGG, WAV, or WebM."
        elif types == DOCUMENT_CONTENT_TYPES:
            hint = "Use PDF."
        else:
            hint = "Use JPEG/PNG/WebP/GIF or PDF for documents, or MP3/M4A/AAC/OGG/WAV/WebM for audio."
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
