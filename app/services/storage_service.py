"""Receipt file storage: Supabase Storage when configured, local disk otherwise."""

import io
import re
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, bad_request, not_found
from app.db.supabase_client import get_supabase

MAX_RECEIPT_BYTES = 8 * 1024 * 1024
IMAGE_FORMATS = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"), "WEBP": (".webp", "image/webp")}
RECEIPT_ROUTE = "/api/v1/expenses/receipts/"


def private_receipt_url(url: str) -> str:
    """Existing local files stay usable through the authenticated route."""
    if url.startswith("/uploads/receipts/"):
        return RECEIPT_ROUTE + url.removeprefix("/uploads/receipts/")
    return url


def _private_bucket(supabase):
    name = get_settings().supabase_storage_bucket
    info = supabase.storage.get_bucket(name)
    if getattr(info, "public", None) is not False:
        raise AppError(503, "receipt_storage_unavailable", "Receipts require a private storage bucket.")
    return supabase.storage.from_(name)


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
        bucket = _private_bucket(supabase)
        bucket.upload(key, data, {"content-type": content_type})
    else:
        path = Path(settings.upload_dir) / "receipts" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return RECEIPT_ROUTE + key


def read_receipt(club_id: uuid.UUID, filename: str) -> bytes:
    """Caller must authorize the stored receipt row before calling. Never fetch arbitrary URLs."""
    if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|webp|pdf)", filename) is None:
        raise not_found("Receipt")
    key = f"{club_id}/{filename}"
    supabase = get_supabase()
    if supabase is not None:
        return _private_bucket(supabase).download(key)
    root = (Path(get_settings().upload_dir) / "receipts").resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise not_found("Receipt")
    return path.read_bytes()
