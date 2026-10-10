"""Receipt file storage: Supabase Storage when configured, local disk otherwise."""

import io
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, bad_request
from app.db.supabase_client import get_supabase

MAX_RECEIPT_BYTES = 8 * 1024 * 1024
IMAGE_FORMATS = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"), "WEBP": (".webp", "image/webp")}


def identify_receipt(data: bytes) -> tuple[str, str]:
    """Work out the real file type from its contents (never from the name or the browser's header).

    Returns (extension, content_type); raises for anything that is not PNG, JPEG, WebP or PDF.
    """
    if not data:
        raise bad_request("empty_file", "The receipt file is empty")
    if len(data) > MAX_RECEIPT_BYTES:
        raise AppError(413, "file_too_large", "Receipts must be 8 MB or smaller")
    if data.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            image.verify()
    except Exception:
        image_format = None
    if image_format not in IMAGE_FORMATS:
        raise AppError(415, "unsupported_file", "Upload the receipt as a PNG, JPEG, WebP or PDF file")
    return IMAGE_FORMATS[image_format]


def save_receipt(club_id: uuid.UUID, data: bytes, extension: str, content_type: str) -> str:
    """Store the file under a random name and return its URL. Blocking: call through a thread pool."""
    settings = get_settings()
    key = f"{club_id}/{uuid.uuid4().hex}{extension}"

    supabase = get_supabase()
    if supabase is not None:
        bucket = supabase.storage.from_(settings.supabase_storage_bucket)
        bucket.upload(key, data, {"content-type": content_type})
        return bucket.get_public_url(key)

    path = Path(settings.upload_dir) / "receipts" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"/uploads/receipts/{key}"
