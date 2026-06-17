import mimetypes
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.xlsx', '.png', '.jpg', '.jpeg'}
ALLOWED_MIME_TYPES = {
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'image/png',
    'image/jpeg',
}


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


def _validate_mime(file: UploadFile) -> None:
    content_type = (file.content_type or '').lower()
    guessed, _ = mimetypes.guess_type(file.filename or '')
    if content_type not in ALLOWED_MIME_TYPES and guessed not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unsupported MIME type')


def save_upload(file: UploadFile, storage_folder: str, file_id: str) -> tuple[str, str, bytes, int]:
    ensure_storage_dirs()
    suffix = _safe_suffix(file.filename or '')
    _validate_mime(file)

    folder_path = ensure_folder((settings.uploads_dir / storage_folder).resolve())
    target_path = folder_path / f'{file_id}{suffix}'

    total_bytes = 0
    payload = bytearray()
    with target_path.open('wb') as handle:
        while chunk := file.file.read(1024 * 1024):
            total_bytes += len(chunk)
            if total_bytes > settings.max_upload_bytes:
                target_path.unlink(missing_ok=True)
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='File too large')
            handle.write(chunk)
            payload.extend(chunk)

    return str(target_path.resolve()), file_id, bytes(payload), total_bytes


def build_result_path(storage_folder: str, file_id: str) -> Path:
    folder_path = ensure_folder((settings.results_dir / storage_folder).resolve())
    return (folder_path / f'{file_id}.md').resolve()


def build_edited_result_path(storage_folder: str, file_id: str, version: int) -> Path:
    edited_dir = ensure_folder((settings.results_dir / storage_folder / 'edited').resolve())
    return (edited_dir / f'{file_id}.v{version}.md').resolve()
