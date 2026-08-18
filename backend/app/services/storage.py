import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.png', '.jpg', '.jpeg', '.eml'}
ALLOWED_MIME_TYPES = {
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
    'image/png',
    'image/jpeg',
    'message/rfc822',
}

_EXTENSION_TO_MIME_TYPES: dict[str, set[str]] = {
    '.pdf': {'application/pdf'},
    '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    '.pptx': {'application/vnd.openxmlformats-officedocument.presentationml.presentation'},
    '.xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
    '.xls': {'application/vnd.ms-excel'},
    '.png': {'image/png'},
    '.jpg': {'image/jpeg'},
    '.jpeg': {'image/jpeg'},
    '.eml': {'message/rfc822'},
}

_GENERIC_MIME_TYPES = {'', 'application/octet-stream', 'binary/octet-stream'}


def ensure_storage_dirs() -> None:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.results_dir.mkdir(parents=True, exist_ok=True)


def ensure_folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported file extension')
    return suffix


def _validate_mime(file: UploadFile, suffix: str) -> None:
    """Reject a declared MIME type that contradicts the file extension.

    The extension is the actual gate -- `_safe_suffix` has already rejected
    anything outside ALLOWED_EXTENSIONS, and this function cannot add much on
    top of it, because the client picks both values. What it does do is catch
    the obvious mismatch (a .pdf announced as image/png).

    Clients routinely send nothing useful here: curl, browser drag/drop and
    sync clients all send application/octet-stream, so a generic or missing
    type is accepted. A type whose top-level category matches the extension
    (image/jpg for .jpg) is accepted too -- that is a client quirk, not a
    contradiction.

    Neither check says anything about the actual bytes. What has to cope with
    hostile input is the parser layer downstream (pypdf, xlrd, PaddleOCR).
    """
    declared = (file.content_type or '').strip().lower()
    expected = _EXTENSION_TO_MIME_TYPES.get(suffix, set())

    if not declared or declared in _GENERIC_MIME_TYPES:
        return
    if declared in expected:
        return
    if expected and declared.split('/')[0] in {entry.split('/')[0] for entry in expected}:
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='MIME type does not match file extension',
    )


def save_upload(file: UploadFile, storage_folder: str, file_id: str) -> tuple[str, str, bytes, int]:
    ensure_storage_dirs()
    suffix = _safe_suffix(file.filename or '')
    _validate_mime(file, suffix)

    folder_path = ensure_folder((settings.uploads_dir / storage_folder).resolve())
    target_path = folder_path / f'{file_id}{suffix}'

    total_bytes = 0
    payload = bytearray()
    oversized = False
    try:
        with target_path.open('wb') as handle:
            while chunk := file.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_bytes:
                    oversized = True
                    break
                handle.write(chunk)
                payload.extend(chunk)
    except Exception:
        # e.g. client disconnect mid-stream: the closed handle has already
        # committed a partial object (on Mountpoint-for-S3, close() commits),
        # so remove it before propagating.
        target_path.unlink(missing_ok=True)
        raise

    # On Mountpoint-for-S3, close() is what commits the object, so the handle
    # must be closed (via the `with` block above) before we unlink the target.
    # Unlinking while the handle is still open would race the close, either
    # raising on the unlink or leaving a partial object behind after close.
    if oversized:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='File too large')

    return str(target_path.resolve()), file_id, bytes(payload), total_bytes


def build_result_path(storage_folder: str, file_id: str) -> Path:
    folder_path = ensure_folder((settings.results_dir / storage_folder).resolve())
    return (folder_path / f'{file_id}.md').resolve()


def build_edited_result_path(storage_folder: str, file_id: str, version: int) -> Path:
    edited_dir = ensure_folder((settings.results_dir / storage_folder / 'edited').resolve())
    return (edited_dir / f'{file_id}.v{version}.md').resolve()
