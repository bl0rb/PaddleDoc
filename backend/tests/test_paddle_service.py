import io
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from app.services import paddle_service
from app.services.quality_gate import evaluate_document_quality


def test_runtime_capability_cpu_selected(monkeypatch):
    monkeypatch.setattr(paddle_service, '_has_torch', lambda: True)
    monkeypatch.setattr(paddle_service, '_has_cuda', lambda: False)

    cap = paddle_service.get_runtime_capability()
    assert cap['selected_device'] == 'cpu'
    assert cap['cuda_available'] is False


def test_convert_to_markdown_with_paddle_backend(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddleocr_to_structure',
        lambda _source, _profile_id, _profile, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'paragraph_title',
                            'block_content': 'Parsed title',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        },
                        {
                            'block_label': 'text',
                            'block_content': 'Parsed text',
                            'block_bbox': [0, 10, 10, 20],
                            'block_id': 2,
                            'block_order': 2,
                        },
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'dt_scores': [0.99, 0.97], 'rec_score': 0.98}},
                        'markdown': {'markdown': 'sample'},
                    }
                ],
                'pdf_chunking': None,
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Parsed title' in markdown
    assert 'Parsed text' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['used_fallback'] is False
    assert details['profile_id'] == 'ppocrv6_tiny'
    assert details['converter'] == 'ppstructure-json-to-rag-markdown'
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}
    assert details['quality_gate']['recommendation'] in {'allow', 'warn', 'block'}


def test_convert_to_markdown_falls_back_to_pypdf_when_paddle_missing(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    class FakePage:
        def __init__(self, text: str):
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        def __init__(self, _path: str):
            self.pages = [FakePage('Hello from PDF')]

    monkeypatch.setattr(paddle_service, 'PdfReader', FakeReader)
    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: False)

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Hello from PDF' in markdown
    assert details['engine'] == 'pypdf-fallback'
    assert details['used_fallback'] is True
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_non_pdf_uses_paddle_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    source.write_bytes(b'test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddleocr_to_structure',
        lambda _source, _profile_id, _profile, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'text',
                            'block_content': 'docx parsed',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        }
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'confidence': 0.99}},
                        'markdown': {'markdown': 'docx parsed'},
                    }
                ],
                'pdf_chunking': None,
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'docx parsed' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_get_paddle_capabilities_exposes_profiles():
    caps = paddle_service.get_paddle_capabilities()
    assert any(profile['value'] == 'ppocrv6_tiny' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_tiny_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'paddlevl_1_6_0_9b' for profile in caps['profiles'])


def test_convert_to_markdown_uses_paddlevl_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddlevl_to_structure',
        lambda _source, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'paragraph_title',
                            'block_content': 'VL title',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        },
                        {
                            'block_label': 'text',
                            'block_content': 'VL text',
                            'block_bbox': [0, 10, 10, 20],
                            'block_id': 2,
                            'block_order': 2,
                        },
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'confidence': 0.99}},
                    }
                ],
                'pdf_chunking': {'enabled': False, 'chunk_page_size': 1},
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='paddlevl_1_6_0_9b')
    assert 'VL title' in markdown
    assert 'VL text' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['used_fallback'] is False
    assert details['profile_id'] == 'paddlevl_1_6_0_9b'
    assert details['converter'] == 'paddlevl-json-to-rag-markdown'


def test_convert_structure_to_markdown_renders_rag_blocks():
    markdown, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'paragraph_title',
                        'block_content': 'Heading',
                        'block_bbox': [1, 2, 3, 4],
                        'block_id': 10,
                        'block_order': 1,
                    },
                    {
                        'block_label': 'table',
                        'block_content': '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>',
                        'block_bbox': [5, 6, 7, 8],
                        'block_id': 11,
                        'block_order': 2,
                    },
                ],
            }
        ],
        source_name='test.pdf',
        profile_label='PP-OCRv6 tiny det + rec',
        metadata={
            'mode': 'collection',
            'email': 'team@example.com',
            'department': 'Sales',
            'profile_id': 'ppocrv6_tiny',
            'engine': 'paddleocr',
            'job_id': 'job-123',
            'document_version': 2,
            'content_sha256': 'abc123',
            'previous_job_id': 'job-122',
            'uploaded_by': 'alice',
            'team': 'Research',
            'tags': ['finance', 'invoices'],
        },
    )

    assert markdown.startswith('---\n')
    assert '## Heading' in markdown
    # yaml.safe_dump renders plain scalars unquoted (no more manual
    # f-string double-quoting).
    assert 'source: test.pdf' in markdown
    assert 'profile_id: ppocrv6_tiny' in markdown
    assert 'mode: collection' in markdown
    assert 'email: team@example.com' in markdown
    assert 'department: Sales' in markdown
    assert 'job_id: job-123' in markdown
    assert 'document_version: 2' in markdown
    assert 'content_sha256: abc123' in markdown
    assert 'previous_job_id: job-122' in markdown
    assert 'uploaded_by: alice' in markdown
    assert 'team: Research' in markdown
    assert 'tags:' in markdown and '- finance' in markdown and '- invoices' in markdown
    assert 'engine: paddleocr' in markdown
    assert 'used_fallback' not in markdown  # only included when true
    # Table rendered as markdown, not raw HTML
    assert '| A | B |' in markdown
    assert '| 1 | 2 |' in markdown
    assert '---' in markdown  # separator present
    assert stats['block_count'] == 2


def test_build_rag_frontmatter_omits_empty_optional_keys():
    frontmatter = paddle_service._build_rag_frontmatter(
        'plain.pdf', 3, 'PP-OCRv6 tiny det + rec', metadata={'engine': 'paddleocr', 'profile_id': 'ppocrv6_tiny'}
    )

    assert frontmatter.startswith('---\n')
    assert frontmatter.rstrip('\n').endswith('---')
    assert 'email:' not in frontmatter
    assert 'department:' not in frontmatter
    assert 'previous_job_id:' not in frontmatter
    assert 'uploaded_by:' not in frontmatter
    assert 'team:' not in frontmatter
    assert 'tags:' not in frontmatter
    assert 'used_fallback' not in frontmatter
    assert 'job_id: null' in frontmatter
    assert 'content_sha256: null' in frontmatter
    assert 'document_version: 1' in frontmatter


def test_build_rag_frontmatter_includes_used_fallback_only_when_true():
    frontmatter = paddle_service._build_rag_frontmatter(
        'plain.pdf', 1, 'pypdf fallback', metadata={'engine': 'pypdf-fallback', 'used_fallback': True}
    )
    assert 'used_fallback: true' in frontmatter


def test_evaluate_document_quality_prefers_clean_high_confidence_documents():
    quality = evaluate_document_quality(
        '# Title\n\nClean document with table content.',
        page_structures=[
            {
                'parsing_res_list': [
                    {'block_label': 'paragraph_title', 'block_content': 'Title', 'block_order': 1},
                    {'block_label': 'table', 'block_content': '| A | B |', 'block_order': 2},
                ]
            }
        ],
        raw_outputs=[{'json': {'res': {'dt_scores': [0.98, 0.97], 'rec_score': 0.99}}}],
        block_stats={'page_count': 1, 'block_count': 2, 'block_labels': {'paragraph_title': 1, 'table': 1}},
    )

    assert quality['grade'] == 'A'
    assert quality['recommendation'] == 'allow'
    assert quality['score'] >= 0.9


def test_evaluate_document_quality_penalizes_noise():
    quality = evaluate_document_quality('@@@ @@ @@@\n@@@ @@ @@@\n@@@ @@ @@@')

    assert quality['grade'] == 'C'
    assert quality['recommendation'] == 'block'


# --- FEATURE: VL connection benchmarking (vl_override) ------------------------
#
# _openai_vision_to_structure requires `import pypdfium2` to succeed as an
# availability check even on the non-PDF (image) path exercised below, which
# never actually calls into it -- pypdfium2 isn't installed in this test
# environment, so a bare, empty stand-in module is injected into
# sys.modules for the duration of each test (the real `import pypdfium2`
# statement resolves from sys.modules first, before touching the loader).

def _install_fake_pypdfium2(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, 'pypdfium2', types.ModuleType('pypdfium2'))


def test_openai_vision_env_based_path_unaffected_when_vl_override_is_none(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    calls = []

    def fake_call(*, api_base, bearer_token, model_name, system_prompt, image_b64, page_num, connection_label):
        calls.append({
            'api_base': api_base,
            'bearer_token': bearer_token,
            'model_name': model_name,
            'system_prompt': system_prompt,
            'page_num': page_num,
            'connection_label': connection_label,
        })
        return 'extracted markdown'

    monkeypatch.setattr(paddle_service, '_call_vision_chat_api', fake_call)
    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', 'https://api.example.com')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', 'env-token')

    page_structures, meta = paddle_service._openai_vision_to_structure(
        source, {'vision_model': 'gpt-4o-mini'}, vl_override=None
    )

    assert len(calls) == 1
    # Byte-identical to the pre-override behavior: env settings and the
    # profile's vision_model/default prompt flow through untouched.
    assert calls[0]['api_base'] == 'https://api.example.com'
    assert calls[0]['bearer_token'] == 'env-token'
    assert calls[0]['model_name'] == 'gpt-4o-mini'
    assert 'precise document OCR' in calls[0]['system_prompt']
    # No VlConnection on the env-based path -- generic, non-secret label.
    assert calls[0]['connection_label'] == 'OpenAI vision (env-configured)'
    assert meta['vision_model'] == 'gpt-4o-mini'
    assert meta['api_base'] == 'https://api.example.com'
    assert page_structures[0]['parsing_res_list'][0]['block_content'] == 'extracted markdown'


def test_openai_vision_vl_override_takes_priority_over_env(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    calls = []

    def fake_call(*, api_base, bearer_token, model_name, system_prompt, image_b64, page_num, connection_label):
        calls.append({
            'api_base': api_base, 'bearer_token': bearer_token,
            'model_name': model_name, 'system_prompt': system_prompt,
            'connection_label': connection_label,
        })
        return 'override markdown'

    monkeypatch.setattr(paddle_service, '_call_vision_chat_api', fake_call)
    # Env is configured too, but the override must win on every field.
    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', 'https://env.example.com')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', 'env-token')

    override = {
        'base_url': 'https://vl-connection.internal:8080/',
        'api_key': 'connection-key',
        'model': 'qwen-vl',
        'system_prompt': 'Custom system prompt for this connection.',
        'name': 'My Internal VL Connection',
    }

    page_structures, meta = paddle_service._openai_vision_to_structure(
        source, {'vision_model': 'gpt-4o'}, vl_override=override
    )

    assert len(calls) == 1
    assert calls[0]['api_base'] == 'https://vl-connection.internal:8080'
    assert calls[0]['bearer_token'] == 'connection-key'
    assert calls[0]['model_name'] == 'qwen-vl'
    assert calls[0]['system_prompt'] == 'Custom system prompt for this connection.'
    # The connection's name, never its base_url, is what error messages show.
    assert calls[0]['connection_label'] == 'My Internal VL Connection'
    assert meta['vision_model'] == 'qwen-vl'
    assert meta['api_base'] == 'https://vl-connection.internal:8080'
    assert page_structures[0]['parsing_res_list'][0]['block_content'] == 'override markdown'


def test_call_vision_chat_api_unreachable_omits_api_base_uses_connection_label(monkeypatch, caplog):
    """The admin-configured base_url (may point at an internal/VPC-only
    host) must never appear in the RuntimeError raised here -- it lands
    verbatim in job.error_message, processing_info.execution.fallback_reason,
    and the benchmark report/export, all readable by any teammate who can
    see the job/run, not just admins. The full URL is still logged for
    operators with worker log access."""
    secret_host = 'https://vl-internal.example.corp:9443'

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    with caplog.at_level('WARNING'):
        with pytest.raises(RuntimeError) as exc_info:
            paddle_service._call_vision_chat_api(
                api_base=secret_host,
                bearer_token='secret-token',
                model_name='m',
                system_prompt='p',
                image_b64='aGk=',
                page_num=1,
                connection_label='Prod VL Connection',
            )

    message = str(exc_info.value)
    assert message == 'VL endpoint "Prod VL Connection" unreachable: connection refused'
    assert secret_host not in message

    # The full URL is available to operators via worker logs.
    assert any(secret_host in record.getMessage() for record in caplog.records)


def test_call_vision_chat_api_http_error_omits_api_base_uses_connection_label(monkeypatch, caplog):
    secret_host = 'https://vl-internal.example.corp:9443'

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, 'Server Error', hdrs=None, fp=io.BytesIO(b'boom'))

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    with caplog.at_level('WARNING'):
        with pytest.raises(RuntimeError) as exc_info:
            paddle_service._call_vision_chat_api(
                api_base=secret_host,
                bearer_token='secret-token',
                model_name='m',
                system_prompt='p',
                image_b64='aGk=',
                page_num=3,
                connection_label='Prod VL Connection',
            )

    message = str(exc_info.value)
    assert message == 'VL endpoint "Prod VL Connection" returned HTTP 500 for page 3: boom'
    assert secret_host not in message
    assert any(secret_host in record.getMessage() for record in caplog.records)


def test_openai_vision_to_structure_propagates_connection_label_not_base_url(monkeypatch, tmp_path):
    """End-to-end wiring check (real `_call_vision_chat_api`, not mocked):
    a VlConnection's base_url must not survive into the RuntimeError that
    `_openai_vision_to_structure` lets propagate -- that message is what
    ultimately becomes job.error_message / fallback_reason and the
    benchmark report/export."""
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    secret_host = 'https://vl-internal.example.corp:9443'

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    override = {
        'base_url': secret_host,
        'api_key': 'k',
        'model': 'm',
        'system_prompt': '',
        'name': 'Prod VL Connection',
    }

    with pytest.raises(RuntimeError) as exc_info:
        paddle_service._openai_vision_to_structure(source, {'vision_model': 'gpt-4o'}, vl_override=override)

    message = str(exc_info.value)
    assert secret_host not in message
    assert 'Prod VL Connection' in message


def test_openai_vision_missing_config_still_raises_with_override_absent(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', '')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', '')

    with pytest.raises(RuntimeError, match='OPENAI_API_BASE_URL'):
        paddle_service._openai_vision_to_structure(source, {}, vl_override=None)


def test_convert_to_markdown_forwards_vl_override_only_for_openai_vision(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny', 'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)

    seen_overrides = []

    def fake_openai_vision(source_arg, profile_arg, *, vl_override=None):
        seen_overrides.append(vl_override)
        return (
            [{'parsing_res_list': [
                {'block_label': 'llm_markdown', 'block_content': 'vl text', 'block_order': 0, 'block_id': 0}
            ]}],
            {'raw_outputs': [], 'pdf_chunking': {'enabled': False, 'chunk_page_size': 1}, 'vision_model': 'x', 'api_base': 'y'},
        )

    monkeypatch.setattr(paddle_service, '_openai_vision_to_structure', fake_openai_vision)

    override = {'base_url': 'https://x', 'api_key': 'k', 'model': 'm', 'system_prompt': ''}
    markdown, details = paddle_service.convert_to_markdown_with_details(
        str(source), profile_id='openai_vision', vl_override=override
    )
    assert seen_overrides == [override]
    assert 'vl text' in markdown


def test_convert_to_markdown_ignores_vl_override_for_non_vl_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny', 'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)

    def fake_ppstructure(_source, _profile_id, _profile, _capability):
        return (
            [{'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'ocr text', 'block_order': 0, 'block_id': 0}
            ]}],
            {'raw_outputs': [], 'pdf_chunking': None},
        )

    # A vl_override accidentally forwarded into the ppstructurev3 path would
    # raise TypeError here (it takes no such kwarg) -- this call succeeding
    # is the proof it was never passed through.
    monkeypatch.setattr(paddle_service, '_paddleocr_to_structure', fake_ppstructure)

    override = {'base_url': 'https://x', 'api_key': 'k', 'model': 'm', 'system_prompt': ''}
    markdown, details = paddle_service.convert_to_markdown_with_details(
        str(source), profile_id='ppocrv6_tiny', vl_override=override
    )
    assert 'ocr text' in markdown
    assert details['engine'] == 'paddleocr'


# --- FEATURE: VL connection admin /test probe (test_vl_connection) ------------

def test_test_vl_connection_success(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices": []}'

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['timeout'] = timeout
        captured['auth'] = req.headers.get('Authorization')
        return FakeResponse()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    result = paddle_service.test_vl_connection(
        'https://vl.example.com/', 'model-x', 'key-x', 'custom prompt', timeout_seconds=5
    )
    assert result == {'ok': True, 'detail': 'Connected', 'latency_ms': result['latency_ms']}
    assert isinstance(result['latency_ms'], int)
    assert result['latency_ms'] >= 0
    assert captured['url'] == 'https://vl.example.com/v1/chat/completions'
    assert captured['timeout'] == 5
    assert captured['auth'] == 'Bearer key-x'


def test_test_vl_connection_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', hdrs=None, fp=io.BytesIO(b'bad key'))

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    result = paddle_service.test_vl_connection('https://vl.example.com', 'model-x', 'bad-key', '')
    assert result['ok'] is False
    assert 'HTTP 401' in result['detail']
    assert 'bad key' in result['detail']
    assert isinstance(result['latency_ms'], int)


def test_test_vl_connection_url_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    result = paddle_service.test_vl_connection('https://unreachable.example.com', 'model-x', 'key', '')
    assert result['ok'] is False
    assert 'Unreachable' in result['detail']
    assert isinstance(result['latency_ms'], int)
