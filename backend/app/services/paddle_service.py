import html
import importlib.util
import platform
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from celery.exceptions import TimeoutError as CeleryTimeoutError
from pypdf import PdfReader, PdfWriter
from redis import Redis

from app.core.config import settings
from app.services.quality_gate import evaluate_document_quality

_RUNTIME_SETTINGS_KEY = 'paddle:runtime_settings'
_DEFAULT_PROFILE_ID = 'ppocrv6_tiny'
_PDF_CHUNK_PAGE_SIZE = 6
_PDF_CHUNK_PAGE_SIZE_BY_PROFILE: dict[str, int] = {
    'ppocrv6_medium_structurev3': 2,
    'ppocrv6_medium': 2,
    'ppocrv6_small_structurev3': 4,
    'ppocrv6_small': 4,
    'ppocrv6_tiny_structurev3': 6,
    'ppocrv6_tiny': 8,
}

_PADDLE_PROFILES: dict[str, dict[str, str]] = {
    'ppocrv6_tiny': {
        'value': 'ppocrv6_tiny',
        'label': 'PP-OCRv6 tiny det + rec',
        'description': 'Fastest OCR preset (det+rec) for CPU-first deployments with minimal memory usage.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_tiny_structurev3': {
        'value': 'ppocrv6_tiny_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 tiny det + rec',
        'description': 'Tiny det+rec with PP-StructureV3 layout parsing for tables/blocks.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_small': {
        'value': 'ppocrv6_small',
        'label': 'PP-OCRv6 small det + rec',
        'description': 'Balanced OCR preset (det+rec). Mapped to the standard PP-OCRv6 model family.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_small_structurev3': {
        'value': 'ppocrv6_small_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 small det + rec',
        'description': 'Small det+rec with PP-StructureV3 for richer structured output.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_medium': {
        'value': 'ppocrv6_medium',
        'label': 'PP-OCRv6 medium det + rec',
        'description': 'Higher-accuracy OCR preset (det+rec) with larger CPU footprint than small/tiny.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_medium_structurev3': {
        'value': 'ppocrv6_medium_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 medium det + rec',
        'description': 'Best structure quality preset: medium det+rec plus PP-StructureV3 for layouts/tables.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'true',
    },
    'paddlevl_1_6_0_9b': {
        'value': 'paddlevl_1_6_0_9b',
        'label': 'PaddleOCR-VL 1.6 (0.9B)',
        'description': 'Vision-language parsing profile for richer document understanding on GPU-enabled deployments.',
        'pipeline': 'paddlevl',
        'use_table_recognition': 'true',
        'text_detection_model_name': 'PaddleOCR-VL-1.6-0.9B',
        'text_recognition_model_name': 'PaddleOCR-VL-1.6-0.9B',
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


def _has_paddle() -> bool:
    return importlib.util.find_spec('paddle') is not None


def _has_cuda() -> bool:
    try:
        import paddle  # noqa: PLC0415

        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return True
    except Exception:
        pass

    try:
        import torch  # noqa: PLC0415
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _runtime_capability() -> dict:
    cuda_available = _has_cuda()
    info: dict = {
        'torch_available': _has_torch(),
        'paddle_available': _has_paddle(),
        'cuda_available': cuda_available,
        'selected_device': 'gpu' if cuda_available else 'cpu',
        'platform': _runtime_platform_label(),
    }
    if not cuda_available:
        info['no_cuda_reason'] = 'CUDA is unavailable in this deployment; OCR runtime will use CPU'
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


def _pdf_page_count(source: Path) -> int:
    reader = PdfReader(str(source))
    return len(reader.pages)


def _to_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not headers:
        return ''
    header_line = '| ' + ' | '.join(headers) + ' |'
    divider_line = '| ' + ' | '.join(['---'] * len(headers)) + ' |'
    body_lines = ['| ' + ' | '.join(row) + ' |' for row in rows]
    return '\n'.join([header_line, divider_line, *body_lines])


def _fallback_spreadsheet_to_markdown(source: Path) -> tuple[str, int, int]:
    import pandas as pd  # noqa: PLC0415

    suffix = source.suffix.lower()
    engine = 'xlrd' if suffix == '.xls' else None
    sheets = pd.read_excel(source, sheet_name=None, dtype=str, engine=engine)

    sections: list[str] = []
    sheet_count = 0
    row_count = 0

    for sheet_name, frame in sheets.items():
        if frame is None:
            continue
        frame = frame.fillna('')
        headers = [str(col).strip() or f'col_{index + 1}' for index, col in enumerate(frame.columns.tolist())]
        rows = [[str(value).replace('\n', ' ').strip() for value in record] for record in frame.values.tolist()]

        if not headers and not rows:
            continue

        table_md = _to_markdown_table(headers, rows)
        sections.append(f'## Sheet: {sheet_name}\n\n{table_md}'.strip())
        sheet_count += 1
        row_count += len(rows)

    if not sections:
        raise RuntimeError('Spreadsheet fallback extraction produced no rows')

    return '\n\n---\n\n'.join(sections), sheet_count, row_count


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


def _adaptive_pdf_chunk_page_size(
    source: Path,
    profile_id: str,
    total_pages: int,
    capability: dict,
) -> tuple[int, dict[str, int | str | bool]]:
    default_chunk = _PDF_CHUNK_PAGE_SIZE_BY_PROFILE.get(profile_id, _PDF_CHUNK_PAGE_SIZE)
    file_size_mb = source.stat().st_size / (1024 * 1024)
    cpu_only = bool(capability.get('selected_device') == 'cpu')

    # Keep quality profile, but reduce chunk size for risky large PDFs on CPU to lower peak memory.
    adaptive_chunk = default_chunk
    if cpu_only and profile_id.startswith('ppocrv6_medium'):
        if total_pages >= 20 or file_size_mb >= 30:
            adaptive_chunk = 1
        elif total_pages >= 12 or file_size_mb >= 18:
            adaptive_chunk = min(adaptive_chunk, 2)
    elif cpu_only and profile_id.startswith('ppocrv6_small'):
        if total_pages >= 40 or file_size_mb >= 45:
            adaptive_chunk = min(adaptive_chunk, 2)
        elif total_pages >= 24 or file_size_mb >= 28:
            adaptive_chunk = min(adaptive_chunk, 3)

    adaptive_chunk = max(1, adaptive_chunk)
    return adaptive_chunk, {
        'enabled': adaptive_chunk != default_chunk,
        'chunk_page_size': adaptive_chunk,
        'default_chunk_page_size': default_chunk,
        'total_pages': total_pages,
        'file_size_mb': int(file_size_mb),
        'cpu_only': cpu_only,
    }


def _paddleocr_to_structure(
    source: Path,
    profile_id: str,
    profile: dict[str, str],
    capability: dict,
) -> tuple[list[dict], dict]:
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
    page_structures: list[dict] = []
    raw_outputs: list[dict] = []

    def _collect_results(pred_results: list) -> None:
        for result in pred_results:
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

    chunking_meta: dict[str, int | str | bool] = {'enabled': False, 'chunk_page_size': _PDF_CHUNK_PAGE_SIZE}

    if source.suffix.lower() == '.pdf':
        reader = PdfReader(str(source))
        total_pages = len(reader.pages)
        if total_pages == 0:
            raise RuntimeError('PDF has no pages to process')

        chunk_page_size, chunking_meta = _adaptive_pdf_chunk_page_size(
            source=source,
            profile_id=profile_id,
            total_pages=total_pages,
            capability=capability,
        )

        with TemporaryDirectory(prefix='paddledock_pdf_chunks_') as tmpdir:
            tmpdir_path = Path(tmpdir)
            for chunk_start in range(0, total_pages, chunk_page_size):
                chunk_end = min(chunk_start + chunk_page_size, total_pages)
                writer = PdfWriter()
                for page_index in range(chunk_start, chunk_end):
                    writer.add_page(reader.pages[page_index])

                chunk_path = tmpdir_path / f'chunk_{chunk_start + 1}_{chunk_end}.pdf'
                with chunk_path.open('wb') as handle:
                    writer.write(handle)

                chunk_results = list(pipeline.predict(str(chunk_path)))
                if not chunk_results:
                    raise RuntimeError(
                        f'PaddleOCR PP-StructureV3 produced no results for PDF chunk {chunk_start + 1}-{chunk_end}'
                    )
                _collect_results(chunk_results)
    else:
        results = list(pipeline.predict(str(source)))
        if not results:
            raise RuntimeError('PaddleOCR PP-StructureV3 produced no results')
        _collect_results(results)

    if not page_structures:
        raise RuntimeError('PaddleOCR PP-StructureV3 returned no structured pages')

    return page_structures, {
        'raw_outputs': raw_outputs,
        'pdf_chunking': chunking_meta,
    }


def _paddlevl_to_structure(
    source: Path,
    capability: dict,
) -> tuple[list[dict], dict]:
    from paddleocr import PaddleOCRVL  # noqa: PLC0415

    device = 'gpu' if capability.get('selected_device') == 'gpu' else 'cpu'
    pipeline = PaddleOCRVL(pipeline_version='v1.6', device=device)

    results = list(pipeline.predict(str(source)))
    if not results:
        raise RuntimeError('PaddleOCR-VL produced no results')

    page_structures: list[dict] = []
    raw_outputs: list[dict] = []

    for result in results:
        result_json = cast(dict, getattr(result, 'json', {}) or {})
        res_payload = cast(dict | None, result_json.get('res')) if isinstance(result_json, dict) else None
        if not isinstance(res_payload, dict):
            continue
        page_structures.append(res_payload)
        raw_outputs.append({'json': result_json})

    if not page_structures:
        raise RuntimeError('PaddleOCR-VL returned no structured pages')

    return page_structures, {
        'raw_outputs': raw_outputs,
        'pdf_chunking': {'enabled': False, 'chunk_page_size': 1},
    }


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
        'paddlevl_1_6_0_9b',
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
            page_count = _pdf_page_count(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'pypdf-fallback',
                'used_fallback': True,
                'fallback_reason': 'PaddleOCR is not installed in this worker image',
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'page_count': page_count,
                'quality_gate': quality_gate,
                **capability,
            }
        if source.suffix.lower() in {'.xls', '.xlsx'}:
            markdown, sheet_count, row_count = _fallback_spreadsheet_to_markdown(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'spreadsheet-fallback',
                'used_fallback': True,
                'fallback_reason': 'PaddleOCR is not installed in this worker image',
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'page_count': max(1, sheet_count),
                'sheet_count': sheet_count,
                'row_count': row_count,
                'quality_gate': quality_gate,
                **capability,
            }
        raise RuntimeError('PaddleOCR is not installed in this worker image')

    try:
        selected_pipeline = selected_profile.get('pipeline', 'ppstructurev3')
        converter = 'ppstructure-json-to-rag-markdown'
        if selected_pipeline == 'paddlevl':
            page_structures, extraction_meta = _paddlevl_to_structure(source, capability)
            converter = 'paddlevl-json-to-rag-markdown'
        else:
            page_structures, extraction_meta = _paddleocr_to_structure(
                source,
                selected_profile_id,
                selected_profile,
                capability,
            )
        markdown, block_stats = _convert_structure_to_markdown(
            page_structures,
            source_name=source.name,
            profile_label=selected_profile['label'],
            metadata=metadata,
        )
        quality_gate = evaluate_document_quality(
            markdown,
            page_structures=page_structures,
            raw_outputs=cast(list[dict], extraction_meta.get('raw_outputs', [])),
            block_stats=block_stats,
        )
        return markdown, {
            'engine': 'paddleocr',
            'used_fallback': False,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'page_count': block_stats['page_count'],
            'profile': selected_profile,
            'structure': {
                'page_count': block_stats['page_count'],
                'block_count': block_stats['block_count'],
                'block_labels': block_stats['block_labels'],
            },
            'quality_gate': quality_gate,
            'pdf_chunking': extraction_meta.get('pdf_chunking'),
            'converter': converter,
            **capability,
        }
    except Exception as exc:
        if source.suffix.lower() == '.pdf':
            markdown = _fallback_pdf_to_markdown(source)
            page_count = _pdf_page_count(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'pypdf-fallback',
                'used_fallback': True,
                'fallback_reason': str(exc),
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'page_count': page_count,
                'quality_gate': quality_gate,
                **capability,
            }
        if source.suffix.lower() in {'.xls', '.xlsx'}:
            markdown, sheet_count, row_count = _fallback_spreadsheet_to_markdown(source)
            quality_gate = evaluate_document_quality(markdown)
            return markdown, {
                'engine': 'spreadsheet-fallback',
                'used_fallback': True,
                'fallback_reason': str(exc),
                'profile_id': selected_profile_id,
                'profile_label': selected_profile['label'],
                'page_count': max(1, sheet_count),
                'sheet_count': sheet_count,
                'row_count': row_count,
                'quality_gate': quality_gate,
                **capability,
            }
        raise


def convert_to_markdown(input_path: str, profile_id: str | None = None) -> str:
    markdown, _ = convert_to_markdown_with_details(input_path, profile_id=profile_id)
    return markdown