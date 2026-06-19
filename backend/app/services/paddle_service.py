import html
import importlib.util
import platform
import re
from pathlib import Path
from typing import cast

from celery.exceptions import TimeoutError as CeleryTimeoutError
from pypdf import PdfReader
from redis import Redis

from app.core.config import settings
from app.services.quality_gate import evaluate_document_quality

_RUNTIME_SETTINGS_KEY = 'paddle:runtime_settings'
_DEFAULT_PROFILE_ID = 'ppocrv6_tiny'

_PADDLE_PROFILES: dict[str, dict[str, str]] = {
    'ppocrv6_tiny': {
        'value': 'ppocrv6_tiny',
        'label': 'PP-OCRv6 tiny det + rec',
        'description': 'Fastest OCR preset (det+rec) for CPU-first deployments with minimal memory usage.',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_tiny_structurev3': {
        'value': 'ppocrv6_tiny_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 tiny det + rec',
        'description': 'Tiny det+rec with PP-StructureV3 layout parsing for tables/blocks.',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_small': {
        'value': 'ppocrv6_small',
        'label': 'PP-OCRv6 small det + rec',
        'description': 'Balanced OCR preset (det+rec). Mapped to the standard PP-OCRv6 model family.',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_small_structurev3': {
        'value': 'ppocrv6_small_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 small det + rec',
        'description': 'Small det+rec with PP-StructureV3 for richer structured output.',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_medium': {
        'value': 'ppocrv6_medium',
        'label': 'PP-OCRv6 medium det + rec',
        'description': 'Higher-accuracy OCR preset (det+rec) with larger CPU footprint than small/tiny.',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_medium_structurev3': {
        'value': 'ppocrv6_medium_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 medium det + rec',
        'description': 'Best structure quality preset: medium det+rec plus PP-StructureV3 for layouts/tables.',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'true',
    },
}


def _default_runtime_settings() -> dict[str, str | int]:
    return {
        'default_profile': settings.paddle_default_profile,
        'timeout_seconds': settings.paddle_timeout_seconds,
    }


def _redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _runtime_platform_label() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _has_torch() -> bool:
    return importlib.util.find_spec('torch') is not None


def _has_cuda() -> bool:
    try:
        import torch  # noqa: PLC0415
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _runtime_capability() -> dict:
    cuda_available = _has_cuda()
    info: dict = {
        'torch_available': _has_torch(),
        'cuda_available': cuda_available,
        'selected_device': 'cpu',
        'platform': _runtime_platform_label(),
    }
    if not cuda_available:
        info['no_cuda_reason'] = 'PaddleOCR runtime is configured for CPU execution in this deployment'
    return info


def get_runtime_capability() -> dict:
    return _runtime_capability()


def _paddleocr_available() -> bool:
    return importlib.util.find_spec('paddleocr') is not None


def is_paddle_available() -> bool:
    return _paddleocr_available()


def _fallback_pdf_to_markdown(source: Path) -> str:
    reader = PdfReader(str(source))
    sections: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or '').strip()
        if not text:
            continue
        sections.append(f'## Page {index}\n\n{text}')

    if not sections:
        raise RuntimeError('PDF fallback extraction produced no text')
    return '\n\n'.join(sections)


def _clean_block_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


def _html_table_to_markdown(table_html: str) -> str:
    """Convert a simple HTML table to GitHub Flavored Markdown table."""
    rows: list[list[str]] = []
    for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE):
        cells: list[str] = []
        for cell_match in re.finditer(r'<t[dh][^>]*>(.*?)</t[dh]>', row_match.group(1), re.DOTALL | re.IGNORECASE):
            cell_text = re.sub(r'<[^>]+>', '', cell_match.group(1))
            cell_text = html.unescape(cell_text)
            cell_text = re.sub(r'\s+', ' ', cell_text).strip()
            cells.append(cell_text or ' ')
        if cells:
            rows.append(cells)

    if not rows:
        return ''

    # Align all rows to the width of the widest row
    max_cols = max(len(row) for row in rows)
    rows = [row + [' '] * (max_cols - len(row)) for row in rows]

    def md_row(cells: list[str]) -> str:
        return '| ' + ' | '.join(cells) + ' |'

    lines = [md_row(rows[0])]
    lines.append('| ' + ' | '.join(['---'] * max_cols) + ' |')
    for row in rows[1:]:
        lines.append(md_row(row))
    return '\n'.join(lines)


def _render_block_content(label: str, content: str, page_number: int) -> str:
    cleaned = _clean_block_text(content)

    if label in {'paragraph_title', 'doc_title'} and cleaned:
        return f'## {cleaned}'
    if label in {'text', 'paragraph', 'content'} and cleaned:
        return cleaned
    if label == 'table_title' and cleaned:
        return f'### {cleaned}'
    if label == 'table':
        if cleaned and '<table' in cleaned.lower():
            md_table = _html_table_to_markdown(cleaned)
            if md_table:
                return md_table
        if cleaned:
            return cleaned
        return ''
    if label in {'figure', 'image'}:
        return f'*[Figure on page {page_number}]*'
    if label in {'header', 'footer', 'footnote', 'aside_text', 'reference'}:
        if cleaned:
            return f'> {cleaned}'
        return ''
    if cleaned:
        return cleaned
    return ''


def _build_rag_frontmatter(
    source_name: str,
    page_count: int,
    profile_label: str,
    metadata: dict[str, str] | None = None,
) -> str:
    safe_name = source_name.replace('"', "'")
    metadata = metadata or {}
    mode = (metadata.get('mode') or 'single').replace('"', "'")
    email = (metadata.get('email') or '').replace('"', "'")
    department = (metadata.get('department') or '').replace('"', "'")

    lines = [
        '---',
        f'source: "{safe_name}"',
        f'pages: {page_count}',
        f'profile: "{profile_label}"',
        f'mode: "{mode}"',
        f'email: "{email}"',
    ]
    if department:
        lines.append(f'department: "{department}"')
    lines.append('---')
    return '\n'.join(lines)


def _convert_structure_to_markdown(
    page_structures: list[dict],
    source_name: str = '',
    profile_label: str = '',
    metadata: dict[str, str] | None = None,
) -> tuple[str, dict]:
    sections: list[str] = []
    block_count = 0
    labels: dict[str, int] = {}
    page_count = len(page_structures)

    frontmatter = _build_rag_frontmatter(source_name, page_count, profile_label, metadata=metadata)
    sections.append(frontmatter)

    for page_index, page in enumerate(page_structures, start=1):
        page_blocks = page.get('parsing_res_list', []) or []
        page_parts: list[str] = []

        ordered_blocks = sorted(
            page_blocks,
            key=lambda item: (
                item.get('block_order') is None,
                item.get('block_order') if item.get('block_order') is not None else 10**9,
                item.get('block_id', 10**9),
            ),
        )

        for block in ordered_blocks:
            label = str(block.get('block_label') or 'unknown')
            labels[label] = labels.get(label, 0) + 1
            rendered = _render_block_content(
                label=label,
                content=str(block.get('block_content') or ''),
                page_number=page_index,
            )
            if rendered:
                page_parts.append(rendered)
                block_count += 1

        if page_parts:
            page_header = f'<!-- page:{page_index}/{page_count} -->'
            sections.append(page_header + '\n\n' + '\n\n'.join(page_parts))

    markdown = ('\n\n---\n\n'.join(sections)).strip()
    if not markdown or markdown == frontmatter.strip():
        raise RuntimeError('Structured PP-Structure conversion produced empty markdown')
    return markdown, {
        'page_count': page_count,
        'block_count': block_count,
        'block_labels': labels,
    }


def _paddleocr_to_structure(source: Path, profile: dict[str, str]) -> tuple[list[dict], list[dict]]:
    from paddleocr import PPStructureV3  # noqa: PLC0415

    use_table_recognition = profile.get('use_table_recognition', 'false').lower() == 'true'

    pipeline = PPStructureV3(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_formula_recognition=False,
        use_table_recognition=use_table_recognition,
        use_seal_recognition=False,
        use_chart_recognition=False,
        text_detection_model_name=profile['text_detection_model_name'],
        text_recognition_model_name=profile['text_recognition_model_name'],
        engine='onnxruntime',
        device='cpu',
    )
    results = list(pipeline.predict(str(source)))
    if not results:
        raise RuntimeError('PaddleOCR PP-StructureV3 produced no results')

    page_structures: list[dict] = []
    raw_outputs: list[dict] = []
    for result in results:
        result_json = cast(dict, result.json)
        result_markdown = cast(dict, result.markdown)
        res_payload = cast(dict | None, result_json.get('res'))
        if not res_payload:
            continue
        page_structures.append(res_payload)
        raw_outputs.append({
            'json': result_json,
            'markdown': result_markdown,
        })

    if not page_structures:
        raise RuntimeError('PaddleOCR PP-StructureV3 returned no structured pages')

    return page_structures, raw_outputs


def get_paddle_status() -> tuple[str, str | None, dict | None]:
    try:
        from app.workers.tasks import probe_paddle

        task = probe_paddle.delay()
        payload = cast(dict[str, str | None], task.get(timeout=12))
        status_name = payload.get('status')
        runtime_fields = {k: v for k, v in payload.items() if k not in ('status', 'detail')}
        if status_name in {'running', 'failed', 'stopped'}:
            return status_name, payload.get('detail'), runtime_fields or None
        return 'failed', 'Unexpected probe payload from worker', None
    except CeleryTimeoutError:
        return 'stopped', 'Worker unavailable or Paddle probe timed out', None
    except Exception as exc:  # pragma: no cover
        return 'failed', str(exc), None


def get_paddle_settings() -> dict[str, str | int]:
    defaults = _default_runtime_settings()
    try:
        payload = _redis_client().hgetall(_RUNTIME_SETTINGS_KEY)
    except Exception:
        payload = {}

    if not payload:
        return defaults

    runtime = dict(defaults)
    if payload.get('default_profile'):
        runtime['default_profile'] = payload['default_profile']
    timeout_value = payload.get('timeout_seconds')
    if timeout_value is not None:
        try:
            runtime['timeout_seconds'] = max(1, int(timeout_value))
        except ValueError:
            runtime['timeout_seconds'] = defaults['timeout_seconds']
    return runtime


def update_paddle_settings(*, default_profile: str, timeout_seconds: int) -> None:
    selected_profile = default_profile.strip() if default_profile.strip() in _PADDLE_PROFILES else _DEFAULT_PROFILE_ID
    payload = {
        'default_profile': selected_profile,
        'timeout_seconds': str(timeout_seconds),
    }
    try:
        _redis_client().hset(_RUNTIME_SETTINGS_KEY, mapping=payload)
    except Exception:
        settings.paddle_default_profile = payload['default_profile']
        settings.paddle_timeout_seconds = timeout_seconds


def get_paddle_capabilities() -> dict[str, list[dict[str, str]]]:
    profile_order = [
        'ppocrv6_tiny',
        'ppocrv6_tiny_structurev3',
        'ppocrv6_small',
        'ppocrv6_small_structurev3',
        'ppocrv6_medium',
        'ppocrv6_medium_structurev3',
    ]
    return {
        'profiles': [
            _PADDLE_PROFILES[profile_id]
            for profile_id in profile_order
            if profile_id in _PADDLE_PROFILES
        ],
    }


def _resolve_profile(profile_id: str | None) -> tuple[str, dict[str, str]]:
    requested_profile = (profile_id or '').strip() or cast(str, get_paddle_settings()['default_profile'])
    if requested_profile not in _PADDLE_PROFILES:
        requested_profile = _DEFAULT_PROFILE_ID
    return requested_profile, _PADDLE_PROFILES[requested_profile]


def convert_to_markdown_with_details(
    input_path: str,
    profile_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> tuple[str, dict]:
    source = Path(input_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f'Input file not found: {source}')

    selected_profile_id, selected_profile = _resolve_profile(profile_id)
    capability = _runtime_capability()

    if not _paddleocr_available():
        if source.suffix.lower() == '.pdf':
            markdown = _fallback_pdf_to_markdown(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'pypdf-fallback',
                'used_fallback': True,
                'fallback_reason': 'PaddleOCR is not installed in this worker image',
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'quality_gate': quality_gate,
                **capability,
            }
        raise RuntimeError('PaddleOCR is not installed in this worker image')

    try:
        page_structures, raw_outputs = _paddleocr_to_structure(source, selected_profile)
        markdown, block_stats = _convert_structure_to_markdown(
            page_structures,
            source_name=source.name,
            profile_label=selected_profile['label'],
            metadata=metadata,
        )
        quality_gate = evaluate_document_quality(
            markdown,
            page_structures=page_structures,
            raw_outputs=raw_outputs,
            block_stats=block_stats,
        )
        return markdown, {
            'engine': 'paddleocr',
            'used_fallback': False,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'profile': selected_profile,
            'structure': {
                'page_count': block_stats['page_count'],
                'block_count': block_stats['block_count'],
                'block_labels': block_stats['block_labels'],
            },
            'quality_gate': quality_gate,
            'converter': 'ppstructure-json-to-rag-markdown',
            **capability,
        }
    except Exception as exc:
        if source.suffix.lower() == '.pdf':
            markdown = _fallback_pdf_to_markdown(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'pypdf-fallback',
                'used_fallback': True,
                'fallback_reason': str(exc),
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'quality_gate': quality_gate,
                **capability,
            }
        raise


def convert_to_markdown(input_path: str, profile_id: str | None = None) -> str:
    markdown, _ = convert_to_markdown_with_details(input_path, profile_id=profile_id)
    return markdown