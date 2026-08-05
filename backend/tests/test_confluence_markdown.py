"""Converter tests (Phase C importer, PR C1): export_view HTML -> markdown.

Golden-style assertions on representative Confluence export_view fragments:
fenced code blocks, GFM tables, artifact image rewriting, info macros,
hostile titles in YAML frontmatter, cross-page link collection and the
end-of-run rewrite pass.
"""

from datetime import datetime, timezone

import pytest
import yaml

from app.services.confluence_markdown import (
    ConversionResult,
    ImageRef,
    convert_page,
    html_to_markdown,
    rewrite_cross_page_links,
    sanitize_filename,
)

BASE = 'https://acme.atlassian.net'
IMPORTED_AT = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _convert(html: str, *, title='Page', page_id='123', version=1) -> ConversionResult:
    return convert_page(
        html,
        base_url=BASE,
        title=title,
        page_id=page_id,
        page_url=f'{BASE}/wiki/spaces/DOCS/pages/{page_id}/Page',
        page_version=version,
        import_run_id='run-1',
        imported_at=IMPORTED_AT,
    )


def _split_frontmatter(markdown: str) -> tuple[dict, str]:
    assert markdown.startswith('---\n')
    end = markdown.index('\n---\n', 3)
    meta = yaml.safe_load(markdown[4:end + 1])
    body = markdown[end + len('\n---\n'):]
    return meta, body


# --- frontmatter --------------------------------------------------------------

def test_frontmatter_contract_and_metadata():
    result = _convert('<h1>Hello</h1><p>World</p>', title='Getting Started', page_id='123', version=7)
    meta, body = _split_frontmatter(result.markdown)
    assert meta == {
        'title': 'Getting Started',
        'source': f'{BASE}/wiki/spaces/DOCS/pages/123/Page',
        'confluence_page_id': '123',
        'confluence_version': 7,
        'imported_at': '2026-08-04T12:00:00Z',
        'import_run': 'run-1',
    }
    assert body.lstrip().startswith('# Hello')
    assert result.markdown.endswith('\n')


@pytest.mark.parametrize(
    'hostile_title',
    [
        'Evil" title: with "quotes"',
        'Line one\n---\nimport_run: spoofed\ntitle: fake',
        "---\n- '",
        'title: |\n  nested',
    ],
)
def test_hostile_title_cannot_break_frontmatter(hostile_title):
    result = _convert('<p>body text</p>', title=hostile_title)
    meta, body = _split_frontmatter(result.markdown)
    # The hostile string survives as data; the provenance keys are ours.
    assert meta['title'] == hostile_title
    assert meta['import_run'] == 'run-1'
    assert len(meta) == 6
    assert 'body text' in body


# --- code blocks --------------------------------------------------------------

def test_syntaxhighlighter_pre_becomes_fenced_block():
    html = (
        '<div class="code panel pdl"><div class="codeContent panelContent pdl">'
        '<pre class="syntaxhighlighter-pre" data-syntaxhighlighter-params="brush: java; gutter: false; theme: Confluence">'
        'public class Foo {\n    int x = 1;\n}</pre>'
        '</div></div>'
    )
    body = _split_frontmatter(_convert(html).markdown)[1]
    assert '```java\npublic class Foo {\n    int x = 1;\n}\n```' in body


def test_pre_code_language_class_preserved():
    html = '<pre><code class="language-python">x = 1</code></pre>'
    body = _split_frontmatter(_convert(html).markdown)[1]
    assert '```python\nx = 1\n```' in body


def test_plain_pre_becomes_unlabeled_fence():
    body = _split_frontmatter(_convert('<pre>plain text\nblock</pre>').markdown)[1]
    assert '```\nplain text\nblock\n```' in body


# --- tables -------------------------------------------------------------------

def test_confluence_table_becomes_gfm_pipe_table():
    html = (
        '<div class="table-wrap"><table class="confluenceTable"><colgroup><col/><col/></colgroup><tbody>'
        '<tr><th class="confluenceTh"><p>Name</p></th><th class="confluenceTh"><p>Value</p></th></tr>'
        '<tr><td class="confluenceTd"><p>alpha</p></td><td class="confluenceTd"><p>1</p></td></tr>'
        '<tr><td class="confluenceTd"><p>beta</p></td><td class="confluenceTd"><p>2</p></td></tr>'
        '</tbody></table></div>'
    )
    body = _split_frontmatter(_convert(html).markdown)[1]
    assert '| Name | Value |' in body
    assert '| --- | --- |' in body
    assert '| alpha | 1 |' in body
    assert '| beta | 2 |' in body


# --- images -------------------------------------------------------------------

def test_attachment_image_rewritten_to_artifacts_and_captured():
    html = f'<p><img src="/wiki/download/attachments/123/Big%20Diagram.png?version=1&amp;api=v2" alt="diagram"></p>'
    result = _convert(html)
    body = _split_frontmatter(result.markdown)[1]
    assert '![diagram](artifacts/Big_Diagram.png)' in body
    assert result.images == [ImageRef(
        url=f'{BASE}/wiki/download/attachments/123/Big%20Diagram.png?version=1&api=v2',
        filename='Big_Diagram.png',
    )]


def test_thumbnail_image_maps_to_attachment_filename():
    html = '<p><img src="/wiki/download/thumbnails/123/chart.png" alt="chart"></p>'
    result = _convert(html)
    assert '![chart](artifacts/chart.png)' in result.markdown
    assert result.images[0].filename == 'chart.png'


def test_capture_attachments_false_leaves_attachment_image_absolute():
    # include_attachments=false runs store no artifacts, so rewriting to
    # artifacts/{name} would guarantee dangling refs; the absolute source URL
    # is the fallback, same as external-host images.
    html = f'<p><img src="{BASE}/wiki/download/attachments/123/chart.png" /></p>'
    result = convert_page(
        html,
        base_url=BASE,
        title='Page',
        page_id='123',
        page_url=f'{BASE}/wiki/spaces/DOCS/pages/123/Page',
        page_version=1,
        import_run_id='run-1',
        imported_at=IMPORTED_AT,
        capture_attachments=False,
    )
    assert 'artifacts/' not in result.markdown
    assert f'{BASE}/wiki/download/attachments/123/chart.png' in result.markdown
    assert result.images == []


def test_external_image_left_absolute_and_not_captured():
    html = '<p><img src="https://cdn.example.com/pic.png" alt="ext"></p>'
    result = _convert(html)
    assert '![ext](https://cdn.example.com/pic.png)' in result.markdown
    assert result.images == []


def test_same_host_non_attachment_image_not_captured():
    html = '<p><img src="/images/icons/emoticons/smile.svg" alt="icon"></p>'
    result = _convert(html)
    assert f'![icon]({BASE}/images/icons/emoticons/smile.svg)' in result.markdown
    assert result.images == []


def test_data_uri_image_dropped():
    result = _convert('<p>before <img src="data:image/png;base64,AAAA" alt="x"> after</p>')
    assert '![' not in result.markdown
    assert 'data:' not in result.markdown
    assert result.images == []


def test_linked_attachment_image_survives_inside_anchor():
    html = (
        f'<p><a href="{BASE}/wiki/download/attachments/123/full.png">'
        '<img src="/wiki/download/thumbnails/123/full.png" alt="full"></a></p>'
    )
    result = _convert(html)
    assert '![full](artifacts/full.png)' in result.markdown


# --- sanitization -------------------------------------------------------------

def test_script_style_iframe_and_handlers_stripped():
    html = (
        '<p onclick="alert(1)">text</p>'
        '<script>alert(2)</script>'
        '<style>p { color: red }</style>'
        '<iframe src="https://evil.example/frame"></iframe>'
        '<form action="/steal"><input name="x"></form>'
    )
    markdown = _convert(html).markdown
    assert 'alert' not in markdown
    assert 'color: red' not in markdown
    assert 'evil.example' not in markdown
    assert 'text' in markdown


def test_javascript_link_unwrapped_to_text():
    result = _convert('<p><a href="javascript:alert(1)">click me</a></p>')
    assert 'click me' in result.markdown
    assert 'javascript:' not in result.markdown
    assert result.linked_page_ids == []


def test_info_macro_becomes_labeled_blockquote():
    html = (
        '<div class="confluence-information-macro confluence-information-macro-warning">'
        '<span class="aui-icon aui-icon-small confluence-information-macro-icon"></span>'
        '<div class="confluence-information-macro-body"><p>Careful now</p></div>'
        '</div>'
    )
    body = _split_frontmatter(_convert(html).markdown)[1]
    assert '> **Warning**' in body
    assert '> Careful now' in body


def test_page_metadata_cruft_removed():
    html = '<div class="page-metadata">Created by admin on Jan 1</div><p>real content</p>'
    markdown = _convert(html).markdown
    assert 'Created by admin' not in markdown
    assert 'real content' in markdown


# --- links / cross-page collection --------------------------------------------

def test_cross_page_links_collected_and_left_absolute():
    html = (
        '<p><a href="/wiki/spaces/DOCS/pages/456/Child+Page">child</a> and '
        '<a href="https://acme.atlassian.net/pages/viewpage.action?pageId=789">legacy</a> and '
        '<a href="https://other.example/pages/456/dup">offsite</a> and '
        '<a href="https://example.com/plain">plain</a></p>'
    )
    result = _convert(html)
    assert result.linked_page_ids == ['456', '789']
    body = _split_frontmatter(result.markdown)[1]
    assert f'[child]({BASE}/wiki/spaces/DOCS/pages/456/Child+Page)' in body
    assert '[legacy](https://acme.atlassian.net/pages/viewpage.action?pageId=789)' in body


def test_relative_link_resolved_against_base():
    result = _convert('<p><a href="/wiki/display/DOCS/Other">other</a></p>')
    assert f'[other]({BASE}/wiki/display/DOCS/Other)' in result.markdown


def test_html_to_markdown_returns_body_without_frontmatter():
    body, images, linked = html_to_markdown('<h2>Sub</h2>', base_url=BASE)
    assert body == '## Sub'
    assert images == []
    assert linked == []


# --- finalize pass ------------------------------------------------------------

def test_rewrite_cross_page_links_maps_imported_pages_only():
    markdown = (
        '---\n'
        f'title: Root\nsource: {BASE}/wiki/spaces/DOCS/pages/123/Root\n'
        '---\n\n'
        f'See [child]({BASE}/wiki/spaces/DOCS/pages/456/Child+Page) and '
        f'[missing]({BASE}/wiki/spaces/DOCS/pages/999/Gone) and '
        f'[legacy]({BASE}/pages/viewpage.action?pageId=789).\n'
    )
    rewritten = rewrite_cross_page_links(markdown, {'456': 'job-a', '789': 'job-b'})
    assert '[child](/jobs/job-a)' in rewritten
    assert '[legacy](/jobs/job-b)' in rewritten
    # Unimported page and the frontmatter provenance URL stay untouched.
    assert f'[missing]({BASE}/wiki/spaces/DOCS/pages/999/Gone)' in rewritten
    assert f'source: {BASE}/wiki/spaces/DOCS/pages/123/Root' in rewritten


def test_rewrite_cross_page_links_skips_images():
    markdown = f'![shot]({BASE}/wiki/spaces/DOCS/pages/456/screen.png)\n'
    assert rewrite_cross_page_links(markdown, {'456': 'job-a'}) == markdown


def test_rewrite_cross_page_links_empty_mapping_is_identity():
    markdown = f'[child]({BASE}/wiki/spaces/DOCS/pages/456/Child)\n'
    assert rewrite_cross_page_links(markdown, {}) == markdown


# --- filenames ----------------------------------------------------------------

@pytest.mark.parametrize(
    'raw,expected',
    [
        ('report.pdf', 'report.pdf'),
        ('../../etc/passwd', 'passwd'),
        ('..\\..\\windows\\system32', 'system32'),
        ('.hidden', 'hidden'),
        ('Big Diagram v2.png', 'Big_Diagram_v2.png'),
        ('evil\x00\x1fname.png', 'evilname.png'),
        ('', 'file'),
        ('...', 'file'),
        ('/', 'file'),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_keeping_extension():
    name = sanitize_filename('a' * 300 + '.png')
    assert len(name) <= 200
    assert name.endswith('.png')
