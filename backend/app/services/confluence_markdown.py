"""Confluence export_view HTML -> Markdown conversion (Phase C importer).

Pipeline (D4/§2.2 of the import design):

1. BeautifulSoup pre-pass on the server-rendered export_view HTML:
   - strip script/style/iframe/form/embed and on* event-handler attributes
     (defense-in-depth -- the frontend renderer never executes raw HTML,
     but the raw view and downloaded .md files must be clean too);
   - normalize Confluence code macros (syntaxhighlighter `<pre>` /
     `div.codeContent`) to `<pre><code class="language-x">` so markdownify
     emits fenced code blocks;
   - resolve `<img>` srcs: same-host attachment/thumbnail downloads are
     rewritten to `artifacts/{filename}` and recorded as ImageRef for the
     worker task (which stores bytes ONLY via the API attachment listing --
     an URL harvested from HTML is provenance, never fetched); external
     images stay absolute; `data:` URIs are dropped;
   - unwrap info/note/warning macros into labeled blockquotes; drop
     page-metadata cruft; drop non-http(s) links (javascript: etc.).
2. markdownify (ATX headings, '-' bullets, GFM pipe tables).
3. YAML frontmatter emitted via yaml.safe_dump only -- a page title
   containing quotes/newlines is attacker-controllable and must not be able
   to break the `---` fencing or spoof provenance keys like `import_run`.

Cross-page links: any href with an extractable numeric pageId is recorded
in ConversionResult.linked_page_ids but left absolute; at end of run the
task calls `rewrite_cross_page_links` with the run's pageId->jobId map to
turn links between imported pages into internal `/jobs/{id}` links.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from app.services.confluence import extract_page_id

_STRIP_TAGS = ('script', 'style', 'iframe', 'form', 'object', 'embed', 'noscript')
_DEFAULT_PORTS = {'http': 80, 'https': 443}
# Cloud: /wiki/download/attachments/{pageId}/{file}; DC: /download/attachments/...;
# export_view frequently embeds images via /download/thumbnails/ -- same
# attachment, scaled, so it maps to the same stored artifact filename.
_ATTACHMENT_PATH_RE = re.compile(r'/download/(?:attachments|thumbnails)/')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')
_EXCESS_BLANK_LINES_RE = re.compile(r'\n{3,}')
# Markdown link destinations: "[text](url)", with the optional leading '!'
# captured so image embeds can be skipped. Frontmatter values never match
# this shape, so the rewrite pass cannot touch the provenance `source:` URL.
_MD_LINK_URL_RE = re.compile(r'(!?)(\[[^\]]*\]\()(https?://[^()\s]+)\)')

_INFO_MACRO_LABELS = {
    'information': 'Info',
    'note': 'Note',
    'warning': 'Warning',
    'tip': 'Tip',
    'error': 'Error',
}


@dataclass(frozen=True)
class ImageRef:
    """An inline image pointing at this Confluence's attachment storage.

    `url` is provenance only -- the task must NOT fetch it (attachment bytes
    come exclusively from the API listing's download links). `filename` is
    the sanitized basename the markdown now references as
    `artifacts/{filename}`; the task matches it against the page's API
    attachment listing when storing artifacts."""

    url: str
    filename: str


@dataclass
class ConversionResult:
    markdown: str  # frontmatter + body; always starts with '---\n'
    images: list[ImageRef]
    linked_page_ids: list[str]  # de-duplicated, document order


def sanitize_filename(name: str) -> str:
    """Storage-safe artifact filename: path components, control characters,
    and leading dots stripped; overlong names truncated (extension kept);
    never empty. Shared contract between the converter's `artifacts/{name}`
    rewrite and the worker's job_artifacts rows -- both sides must produce
    identical names for the markdown to reference stored artifacts."""
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = _CONTROL_CHARS_RE.sub('', name)
    name = name.strip().lstrip('.').strip()
    # Spaces would break the `![...](artifacts/{name})` markdown destination.
    name = re.sub(r'\s+', '_', name)
    if len(name) > 200:
        stem, dot, ext = name.rpartition('.')
        if dot and 0 < len(ext) <= 16:
            name = stem[: 200 - len(ext) - 1].rstrip('.') + '.' + ext
        else:
            name = name[:200]
    return name or 'file'


def _host_key(parts) -> tuple[str, int | None]:
    hostname = (parts.hostname or '').lower()
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    return hostname, port


def _code_language(pre) -> str:
    params = pre.get('data-syntaxhighlighter-params') or ''
    match = re.search(r'brush:\s*([A-Za-z0-9_+#-]+)', params)
    if match:
        return match.group(1).lower()
    for el in (pre.find('code'), pre):
        if el is None:
            continue
        for cls in el.get('class') or []:
            if cls.startswith('language-'):
                return cls[len('language-'):]
    return ''


def _markdownify_language_callback(pre) -> str:
    code = pre.find('code') if hasattr(pre, 'find') else None
    if code is not None:
        for cls in code.get('class') or []:
            if cls.startswith('language-'):
                return cls[len('language-'):]
    return ''


def _normalize_code_blocks(soup: BeautifulSoup) -> None:
    for pre in soup.find_all('pre'):
        language = _code_language(pre)
        replacement = soup.new_tag('pre')
        code = soup.new_tag('code')
        if language:
            code['class'] = [f'language-{language}']
        code.string = pre.get_text()
        replacement.append(code)
        pre.replace_with(replacement)


def _unwrap_info_macros(soup: BeautifulSoup) -> None:
    for macro in soup.find_all('div', class_='confluence-information-macro'):
        label = 'Note'
        for cls in macro.get('class') or []:
            suffix = cls.removeprefix('confluence-information-macro-')
            if suffix != cls and suffix in _INFO_MACRO_LABELS:
                label = _INFO_MACRO_LABELS[suffix]
        for icon in macro.find_all('span', class_='confluence-information-macro-icon'):
            icon.decompose()
        title_el = macro.find(class_='title') or macro.find(class_='confluence-information-macro-title')
        if title_el is not None:
            title_text = title_el.get_text(strip=True)
            if title_text:
                label = f'{label}: {title_text}'
            title_el.decompose()
        blockquote = soup.new_tag('blockquote')
        heading = soup.new_tag('p')
        strong = soup.new_tag('strong')
        strong.string = label
        heading.append(strong)
        blockquote.append(heading)
        body = macro.find('div', class_='confluence-information-macro-body')
        for child in list((body or macro).children):
            blockquote.append(child.extract())
        macro.replace_with(blockquote)


def _prepare(
    soup: BeautifulSoup, *, base_url: str, capture_attachments: bool = True
) -> tuple[list[ImageRef], list[str]]:
    base_parts = urlsplit(base_url)
    base_host = _host_key(base_parts)

    for el in soup.find_all(_STRIP_TAGS):
        el.decompose()
    for el in soup.find_all(class_='page-metadata'):
        el.decompose()
    for el in soup.find_all(True):
        for attr in [name for name in el.attrs if name.lower().startswith('on')]:
            del el.attrs[attr]

    _normalize_code_blocks(soup)
    _unwrap_info_macros(soup)

    linked_page_ids: list[str] = []
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if not href or href.startswith('#'):
            continue
        scheme = urlsplit(href).scheme.lower()
        if scheme and scheme not in ('http', 'https'):
            # javascript:, data:, vbscript:, ... -- keep the text, drop the link.
            anchor.unwrap()
            continue
        absolute = urljoin(base_url + '/', href)
        anchor['href'] = absolute
        page_ref = extract_page_id(absolute)
        if page_ref and page_ref not in linked_page_ids:
            linked_page_ids.append(page_ref)

    images: list[ImageRef] = []
    seen_filenames: set[str] = set()
    for img in soup.find_all('img'):
        src = (img.get('src') or '').strip()
        if not src or src.startswith('data:'):
            img.decompose()
            continue
        absolute = urljoin(base_url + '/', src)
        parts = urlsplit(absolute)
        if parts.scheme.lower() not in ('http', 'https'):
            img.decompose()
            continue
        # Only rewrite to artifacts/{name} when the run will actually store
        # artifacts (include_attachments on) -- otherwise every rewritten ref
        # would dangle; the absolute source URL is the acceptable fallback,
        # same treatment as external-host images.
        if capture_attachments and _host_key(parts) == base_host and _ATTACHMENT_PATH_RE.search(parts.path):
            filename = sanitize_filename(unquote(parts.path.rsplit('/', 1)[-1]))
            img['src'] = f'artifacts/{filename}'
            if filename not in seen_filenames:
                seen_filenames.add(filename)
                images.append(ImageRef(url=absolute, filename=filename))
        else:
            img['src'] = absolute

    return images, linked_page_ids


def html_to_markdown(
    html: str, *, base_url: str, capture_attachments: bool = True
) -> tuple[str, list[ImageRef], list[str]]:
    """Convert export_view HTML to a markdown body (no frontmatter).
    Returns (markdown, images, linked_page_ids)."""
    soup = BeautifulSoup(html, 'html.parser')
    images, linked_page_ids = _prepare(soup, base_url=base_url, capture_attachments=capture_attachments)
    converter = MarkdownConverter(
        heading_style='ATX',
        bullets='-',
        code_language_callback=_markdownify_language_callback,
        # Without this, an image wrapped in a link (Confluence's default
        # embedded-file markup) degrades to bare alt text.
        keep_inline_images_in=['a', 'span', 'td', 'th', 'li'],
    )
    markdown = converter.convert_soup(soup)
    markdown = _EXCESS_BLANK_LINES_RE.sub('\n\n', markdown).strip()
    return markdown, images, linked_page_ids


def render_frontmatter(meta: dict) -> str:
    # safe_dump, never f-string interpolation: attacker-controlled titles
    # must not be able to close the '---' fence or inject sibling keys.
    dumped = yaml.safe_dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f'---\n{dumped}---\n\n'


def convert_page(
    html: str,
    *,
    base_url: str,
    title: str,
    page_id: str,
    page_url: str,
    page_version: int,
    import_run_id: str,
    imported_at: datetime | None = None,
    capture_attachments: bool = True,
) -> ConversionResult:
    """Full conversion for one page: pre-pass + markdownify + frontmatter.
    The result's markdown satisfies the `PUT /jobs/{id}/save` contract
    (starts with '---\\n'). `capture_attachments=False` (a run with
    include_attachments off) leaves same-host attachment images as absolute
    URLs instead of artifacts/ refs that would never resolve."""
    body, images, linked_page_ids = html_to_markdown(
        html, base_url=base_url, capture_attachments=capture_attachments
    )
    moment = (imported_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    meta = {
        'title': title,
        'source': page_url,
        'confluence_page_id': str(page_id),
        'confluence_version': int(page_version),
        'imported_at': moment.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'import_run': import_run_id,
    }
    markdown = render_frontmatter(meta) + body + '\n'
    return ConversionResult(markdown=markdown, images=images, linked_page_ids=linked_page_ids)


def rewrite_cross_page_links(markdown: str, page_id_to_job_id: Mapping[str, str]) -> str:
    """End-of-run finalize pass: rewrite markdown link destinations whose
    Confluence pageId was imported in this run to internal `/jobs/{job_id}`
    links. Image destinations (`![...](...)`) and links to pages outside
    the run are left untouched; frontmatter never matches the link shape."""
    if not page_id_to_job_id:
        return markdown

    def _replace(match: re.Match) -> str:
        if match.group(1) == '!':
            return match.group(0)
        page_ref = extract_page_id(match.group(3))
        job_id = page_id_to_job_id.get(page_ref) if page_ref else None
        if job_id is None:
            return match.group(0)
        return f'{match.group(2)}/jobs/{job_id})'

    return _MD_LINK_URL_RE.sub(_replace, markdown)
