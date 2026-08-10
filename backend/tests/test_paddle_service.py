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
