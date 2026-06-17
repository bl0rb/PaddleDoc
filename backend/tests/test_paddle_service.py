from pathlib import Path

import pytest

from app.services import paddle_service


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
        lambda _source, _profile: (
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
            {'page_markdown': []},
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Parsed title' in markdown
    assert 'Parsed text' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['used_fallback'] is False
    assert details['profile_id'] == 'ppocrv6_tiny'
    assert details['converter'] == 'ppstructure-json-to-rag-markdown'


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
        lambda _source, _profile: (
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
            {'page_markdown': []},
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'docx parsed' in markdown
    assert details['engine'] == 'paddleocr'


def test_get_paddle_capabilities_exposes_profiles():
    caps = paddle_service.get_paddle_capabilities()
    assert any(profile['value'] == 'ppocrv6_tiny' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_tiny_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium_structurev3' for profile in caps['profiles'])


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
        metadata={'mode': 'collection', 'email': 'team@example.com', 'department': 'Sales'},
    )

    assert '## Heading' in markdown
    assert 'mode: "collection"' in markdown
    assert 'email: "team@example.com"' in markdown
    assert 'department: "Sales"' in markdown
    # Table rendered as markdown, not raw HTML
    assert '| A | B |' in markdown
    assert '| 1 | 2 |' in markdown
    assert '---' in markdown  # separator present
    assert stats['block_count'] == 2
