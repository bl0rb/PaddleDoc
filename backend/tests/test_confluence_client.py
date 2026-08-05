"""Confluence REST client tests (Phase C importer, PR C1).

All HTTP is faked at the safe_fetch seam: the clients must route every
request through app.services.safe_fetch.safe_fetch (SSRF enforcement lives
inside it), so the tests monkeypatch exactly that name inside
app.services.confluence and assert on the recorded calls -- URLs, auth
headers, caps, and allowed_private_hosts propagation.
"""

import base64
import json

import pytest

from app.services import confluence as confluence_module
from app.services.confluence import (
    API_BASE_PATH_CLOUD,
    API_BASE_PATH_DATACENTER,
    SERVER_KIND_CLOUD,
    SERVER_KIND_DATACENTER,
    ConfluenceError,
    ConfluenceV1Client,
    ConfluenceV2Client,
    build_auth_header,
    create_client,
    detect_server_kind,
    extract_page_id,
)
from app.services.safe_fetch import SafeFetchError, SafeFetchResponse

CLOUD_BASE = 'https://acme.atlassian.net'
DC_BASE = 'https://wiki.corp.internal:8090/confluence'
SECRET = 'secret-token-value'


def _json_body(payload) -> bytes:
    return json.dumps(payload).encode('utf-8')


class _FakeFetch:
    """Canned safe_fetch: routes exact URL -> (status, body bytes) or an
    exception instance; records every call for assertions."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, *, method='GET', headers=None, body=None, timeout=5.0,
                 max_redirects=5, max_bytes=2 * 1024 * 1024, allowed_private_hosts=None):
        self.calls.append({
            'url': url,
            'headers': dict(headers or {}),
            'timeout': timeout,
            'max_bytes': max_bytes,
            'allowed_private_hosts': allowed_private_hosts,
        })
        if url not in self.routes:
            raise AssertionError(f'unexpected URL fetched: {url}')
        handler = self.routes[url]
        if isinstance(handler, Exception):
            raise handler
        status, response_body = handler
        return SafeFetchResponse(status_code=status, headers={}, body=response_body, final_url=url)

    @property
    def urls(self):
        return [call['url'] for call in self.calls]


def _install(monkeypatch, routes) -> _FakeFetch:
    fake = _FakeFetch(routes)
    monkeypatch.setattr(confluence_module, 'safe_fetch', fake)
    return fake


def _cloud_client(**kwargs) -> ConfluenceV2Client:
    defaults = dict(auth_type='cloud_basic', auth_username='me@acme.test', credential=SECRET)
    defaults.update(kwargs)
    return ConfluenceV2Client(CLOUD_BASE, **defaults)


def _dc_client(**kwargs) -> ConfluenceV1Client:
    defaults = dict(auth_type='pat_bearer', credential=SECRET)
    defaults.update(kwargs)
    return ConfluenceV1Client(DC_BASE, **defaults)


# --- auth headers -------------------------------------------------------------

def test_build_auth_header_cloud_basic():
    header = build_auth_header('cloud_basic', 'me@acme.test', 'api-token')
    expected = base64.b64encode(b'me@acme.test:api-token').decode('ascii')
    assert header == {'Authorization': f'Basic {expected}'}


def test_build_auth_header_pat_bearer():
    assert build_auth_header('pat_bearer', '', 'my-pat') == {'Authorization': 'Bearer my-pat'}


@pytest.mark.parametrize(
    'auth_type,username,credential',
    [
        ('oauth', 'x', 'y'),          # unknown auth type
        ('cloud_basic', '', 'y'),     # cloud_basic without an email
        ('pat_bearer', '', ''),       # missing credential
    ],
)
def test_build_auth_header_rejects_bad_input(auth_type, username, credential):
    with pytest.raises(ConfluenceError):
        build_auth_header(auth_type, username, credential)


# --- page id extraction -------------------------------------------------------

@pytest.mark.parametrize(
    'value,expected',
    [
        ('123456', '123456'),
        ('  123  ', '123'),
        ('https://acme.atlassian.net/wiki/spaces/DOCS/pages/456/Some+Title', '456'),
        ('https://acme.atlassian.net/wiki/spaces/DOCS/pages/456', '456'),
        ('https://wiki.corp.internal:8090/confluence/pages/viewpage.action?pageId=789', '789'),
        ('https://dc.example/pages/viewpage.action?spaceKey=X&pageId=789#anchor', '789'),
        ('https://acme.atlassian.net/wiki/spaces/DOCS/pages/edit-v2/456', None),
        ('https://dc.example/display/DOCS/Some+Title', None),
        ('DOCS', None),
        ('', None),
    ],
)
def test_extract_page_id(value, expected):
    assert extract_page_id(value) == expected


# --- server-kind detection ----------------------------------------------------

def test_detect_cloud(monkeypatch):
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1': (200, _json_body({'results': []})),
    })
    allowlist = frozenset({'acme.atlassian.net'})
    kind, api_base = detect_server_kind(
        CLOUD_BASE, auth_type='cloud_basic', auth_username='me@acme.test',
        credential=SECRET, allowed_private_hosts=allowlist, timeout=7.0, max_bytes=1234,
    )
    assert (kind, api_base) == (SERVER_KIND_CLOUD, API_BASE_PATH_CLOUD)
    call = fake.calls[0]
    assert call['headers']['Authorization'].startswith('Basic ')
    assert call['headers']['Accept'] == 'application/json'
    assert call['allowed_private_hosts'] == allowlist
    assert call['timeout'] == 7.0
    assert call['max_bytes'] == 1234


def test_detect_datacenter(monkeypatch):
    fake = _install(monkeypatch, {
        f'{DC_BASE}/wiki/api/v2/spaces?limit=1': (404, b'not found'),
        f'{DC_BASE}/rest/api/space?limit=1': (200, _json_body({'results': [], 'start': 0})),
    })
    kind, api_base = detect_server_kind(DC_BASE, auth_type='pat_bearer', credential=SECRET)
    assert (kind, api_base) == (SERVER_KIND_DATACENTER, API_BASE_PATH_DATACENTER)
    assert fake.calls[1]['headers']['Authorization'] == f'Bearer {SECRET}'


def test_detect_base_url_with_wiki_suffix_not_doubled(monkeypatch):
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1': (200, _json_body({'results': []})),
    })
    kind, _ = detect_server_kind(
        f'{CLOUD_BASE}/wiki', auth_type='cloud_basic', auth_username='me@acme.test', credential=SECRET
    )
    assert kind == SERVER_KIND_CLOUD
    assert fake.urls == [f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1']


def test_detect_auth_failure(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1': (401, b'{}'),
        f'{CLOUD_BASE}/rest/api/space?limit=1': (404, b''),
    })
    with pytest.raises(ConfluenceError) as excinfo:
        detect_server_kind(CLOUD_BASE, auth_type='cloud_basic', auth_username='me@acme.test', credential=SECRET)
    assert 'authentication failed' in str(excinfo.value)
    assert excinfo.value.status_code == 401
    assert SECRET not in str(excinfo.value)


def test_detect_not_confluence(monkeypatch):
    # A 200 with a non-Confluence body (HTML) must not be detected as Cloud.
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1': (200, b'<html>hello</html>'),
        f'{CLOUD_BASE}/rest/api/space?limit=1': (404, b''),
    })
    with pytest.raises(ConfluenceError) as excinfo:
        detect_server_kind(CLOUD_BASE, auth_type='pat_bearer', credential=SECRET)
    message = str(excinfo.value)
    assert 'not recognized as a Confluence server' in message
    assert '200' in message and '404' in message


def test_detect_unreachable(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?limit=1': SafeFetchError('resolves to a blocked address'),
    })
    with pytest.raises(ConfluenceError) as excinfo:
        detect_server_kind(CLOUD_BASE, auth_type='pat_bearer', credential=SECRET)
    assert 'could not reach' in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


# --- client factory -----------------------------------------------------------

def test_create_client_selects_implementation():
    cloud = create_client(base_url=CLOUD_BASE, server_kind='cloud', auth_type='cloud_basic',
                          auth_username='me@acme.test', credential=SECRET)
    datacenter = create_client(base_url=DC_BASE, server_kind='datacenter', auth_type='pat_bearer',
                               credential=SECRET)
    assert isinstance(cloud, ConfluenceV2Client)
    assert isinstance(datacenter, ConfluenceV1Client)


def test_create_client_rejects_untested_source():
    with pytest.raises(ConfluenceError, match='server kind'):
        create_client(base_url=CLOUD_BASE, server_kind='', auth_type='pat_bearer', credential=SECRET)


# --- v2 (Cloud) ---------------------------------------------------------------

def test_v2_fetch_page(monkeypatch):
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123?body-format=export_view': (200, _json_body({
            'id': '123',
            'title': 'Getting Started',
            'version': {'number': 7},
            'body': {'export_view': {'value': '<h1>Hi</h1>'}},
            '_links': {'webui': '/spaces/DOCS/pages/123/Getting+Started'},
        })),
    })
    client = _cloud_client(timeout=9.0, max_response_bytes=4096,
                           allowed_private_hosts=frozenset({'acme.atlassian.net'}))
    page = client.fetch_page('123')
    assert page.id == '123'
    assert page.title == 'Getting Started'
    assert page.version == 7
    assert page.html == '<h1>Hi</h1>'
    assert page.url == f'{CLOUD_BASE}/wiki/spaces/DOCS/pages/123/Getting+Started'
    call = fake.calls[0]
    assert call['headers']['Authorization'].startswith('Basic ')
    assert call['timeout'] == 9.0
    assert call['max_bytes'] == 4096
    assert call['allowed_private_hosts'] == frozenset({'acme.atlassian.net'})


def test_v2_fetch_page_missing_body(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123?body-format=export_view': (200, _json_body({'id': '123'})),
    })
    with pytest.raises(ConfluenceError, match='export_view'):
        _cloud_client().fetch_page('123')


def test_v2_fetch_page_http_error_maps_status_without_credential(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123?body-format=export_view': (500, b'boom'),
    })
    with pytest.raises(ConfluenceError) as excinfo:
        _cloud_client().fetch_page('123')
    assert excinfo.value.status_code == 500
    assert 'HTTP 500' in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    assert 'Authorization' not in str(excinfo.value)


def test_v2_invalid_json(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123?body-format=export_view': (200, b'not json'),
    })
    with pytest.raises(ConfluenceError, match='not valid JSON'):
        _cloud_client().fetch_page('123')


def test_v2_safe_fetch_error_wrapped(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123?body-format=export_view': SafeFetchError('blocked address'),
    })
    with pytest.raises(ConfluenceError, match='blocked address'):
        _cloud_client().fetch_page('123')


def test_v2_iter_children_follows_cursor(monkeypatch):
    next_path = '/wiki/api/v2/pages/123/children?limit=50&cursor=abc'
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/children?limit=50': (200, _json_body({
            'results': [{'id': '1'}, {'id': '2'}],
            '_links': {'next': next_path},
        })),
        f'{CLOUD_BASE}{next_path}': (200, _json_body({'results': [{'id': '3'}]})),
    })
    assert list(_cloud_client().iter_children('123')) == ['1', '2', '3']
    assert fake.urls == [
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/children?limit=50',
        f'{CLOUD_BASE}{next_path}',
    ]


def test_v2_iter_children_offsite_next_rejected(monkeypatch):
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/children?limit=50': (200, _json_body({
            'results': [{'id': '1'}],
            '_links': {'next': 'https://evil.example/steal'},
        })),
    })
    with pytest.raises(ConfluenceError, match='off the source host'):
        list(_cloud_client().iter_children('123'))
    # The offsite URL itself was never fetched.
    assert fake.urls == [f'{CLOUD_BASE}/wiki/api/v2/pages/123/children?limit=50']


def test_v2_iter_children_repeated_cursor_stops(monkeypatch):
    # A hostile server returning a next link pointing at the same URL must
    # not loop the client forever.
    url = f'{CLOUD_BASE}/wiki/api/v2/pages/123/children?limit=50'
    fake = _install(monkeypatch, {
        url: (200, _json_body({'results': [{'id': '1'}], '_links': {'next': '/wiki/api/v2/pages/123/children?limit=50'}})),
    })
    assert list(_cloud_client().iter_children('123')) == ['1']
    assert fake.urls == [url]


def test_v2_iter_attachments(monkeypatch):
    download_path = '/download/attachments/123/report.pdf?version=1&api=v2'
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/attachments?limit=50': (200, _json_body({
            'results': [{
                'id': 'att-1',
                'title': 'report.pdf',
                'mediaType': 'application/pdf',
                'fileSize': 1234,
                'downloadLink': download_path,
            }],
        })),
        f'{CLOUD_BASE}/wiki{download_path}': (200, b'%PDF-1.4 bytes'),
    })
    client = _cloud_client(max_attachment_bytes=999)
    metas = list(client.iter_attachments('123'))
    assert len(metas) == 1
    meta = metas[0]
    assert meta.id == 'att-1'
    assert meta.filename == 'report.pdf'
    assert meta.media_type == 'application/pdf'
    assert meta.size_bytes == 1234
    assert meta.page_id == '123'
    assert meta.download_url == f'{CLOUD_BASE}/wiki{download_path}'
    assert meta.fetch_bytes() == b'%PDF-1.4 bytes'
    download_call = fake.calls[-1]
    assert download_call['headers']['Authorization'].startswith('Basic ')
    assert download_call['max_bytes'] == 999


def test_v2_iter_attachments_per_page_cap(monkeypatch):
    items = [
        {'id': f'att-{i}', 'title': f'f{i}.png', 'mediaType': 'image/png',
         'fileSize': 10, 'downloadLink': f'/download/attachments/123/f{i}.png'}
        for i in range(5)
    ]
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/attachments?limit=50': (200, _json_body({'results': items})),
    })
    client = _cloud_client(max_attachments_per_page=3)
    assert [meta.id for meta in client.iter_attachments('123')] == ['att-0', 'att-1', 'att-2']


def test_v2_attachment_offsite_download_link_rejected(monkeypatch):
    fake = _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/pages/123/attachments?limit=50': (200, _json_body({
            'results': [{'id': 'att-1', 'title': 'x', 'downloadLink': 'https://evil.example/x'}],
        })),
    })
    with pytest.raises(ConfluenceError, match='off the source host'):
        list(_cloud_client().iter_attachments('123'))
    assert len(fake.calls) == 1  # listing only; the offsite URL never fetched


def test_v2_resolve_space_root(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?keys=DOCS&limit=1': (200, _json_body({
            'results': [{'id': '9', 'key': 'DOCS', 'homepageId': '777'}],
        })),
    })
    assert _cloud_client().resolve_space_root('DOCS') == '777'


def test_v2_resolve_space_root_not_found(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?keys=NOPE&limit=1': (200, _json_body({'results': []})),
    })
    with pytest.raises(ConfluenceError, match='not found'):
        _cloud_client().resolve_space_root('NOPE')


def test_v2_resolve_space_root_no_homepage(monkeypatch):
    _install(monkeypatch, {
        f'{CLOUD_BASE}/wiki/api/v2/spaces?keys=DOCS&limit=1': (200, _json_body({
            'results': [{'id': '9', 'key': 'DOCS', 'homepageId': None}],
        })),
    })
    with pytest.raises(ConfluenceError, match='no homepage'):
        _cloud_client().resolve_space_root('DOCS')


# --- v1 (Server/DC) -----------------------------------------------------------

def test_v1_fetch_page(monkeypatch):
    fake = _install(monkeypatch, {
        f'{DC_BASE}/rest/api/content/42?expand=body.export_view,version,space': (200, _json_body({
            'id': '42',
            'title': 'Runbook',
            'version': {'number': 3},
            'body': {'export_view': {'value': '<p>ops</p>'}},
            '_links': {'webui': '/display/DOCS/Runbook'},
        })),
    })
    page = _dc_client().fetch_page('42')
    assert page.id == '42'
    assert page.title == 'Runbook'
    assert page.version == 3
    assert page.html == '<p>ops</p>'
    assert page.url == f'{DC_BASE}/display/DOCS/Runbook'
    assert fake.calls[0]['headers']['Authorization'] == f'Bearer {SECRET}'


def test_v1_iter_children_start_limit_pagination(monkeypatch):
    first_page = [{'id': str(i)} for i in range(50)]
    fake = _install(monkeypatch, {
        f'{DC_BASE}/rest/api/content/42/child/page?start=0&limit=50': (200, _json_body({'results': first_page})),
        f'{DC_BASE}/rest/api/content/42/child/page?start=50&limit=50': (200, _json_body({'results': [{'id': '50'}]})),
    })
    ids = list(_dc_client().iter_children('42'))
    assert ids == [str(i) for i in range(51)]
    assert fake.urls == [
        f'{DC_BASE}/rest/api/content/42/child/page?start=0&limit=50',
        f'{DC_BASE}/rest/api/content/42/child/page?start=50&limit=50',
    ]


def test_v1_iter_children_short_page_stops(monkeypatch):
    fake = _install(monkeypatch, {
        f'{DC_BASE}/rest/api/content/42/child/page?start=0&limit=50': (200, _json_body({
            'results': [{'id': '1'}, {'id': '2'}],
        })),
    })
    assert list(_dc_client().iter_children('42')) == ['1', '2']
    assert len(fake.calls) == 1


def test_v1_iter_attachments(monkeypatch):
    download_path = '/download/attachments/42/diagram.png?version=2'
    fake = _install(monkeypatch, {
        f'{DC_BASE}/rest/api/content/42/child/attachment?start=0&limit=50&expand=metadata': (200, _json_body({
            'results': [{
                'id': 'att-9',
                'title': 'diagram.png',
                'metadata': {'mediaType': 'image/png'},
                'extensions': {'fileSize': 555},
                '_links': {'download': download_path},
            }],
        })),
        f'{DC_BASE}{download_path}': (200, b'\x89PNG bytes'),
    })
    metas = list(_dc_client().iter_attachments('42'))
    assert len(metas) == 1
    meta = metas[0]
    assert meta.media_type == 'image/png'
    assert meta.size_bytes == 555
    assert meta.download_url == f'{DC_BASE}{download_path}'
    assert meta.fetch_bytes() == b'\x89PNG bytes'
    assert fake.calls[-1]['headers']['Authorization'] == f'Bearer {SECRET}'


def test_v1_resolve_space_root(monkeypatch):
    _install(monkeypatch, {
        f'{DC_BASE}/rest/api/space/DOCS?expand=homepage': (200, _json_body({
            'key': 'DOCS', 'homepage': {'id': '4242'},
        })),
    })
    assert _dc_client().resolve_space_root('DOCS') == '4242'


def test_v1_resolve_space_root_not_found(monkeypatch):
    _install(monkeypatch, {
        f'{DC_BASE}/rest/api/space/NOPE?expand=homepage': (404, b''),
    })
    with pytest.raises(ConfluenceError, match='not found'):
        _dc_client().resolve_space_root('NOPE')
