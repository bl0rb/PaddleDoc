from starlette.requests import Request

from app.services.security import _client_id_from_request


def _make_request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None = None) -> Request:
    scope = {
        'type': 'http',
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': '/',
        'raw_path': b'/',
        'query_string': b'',
        'headers': headers,
        'client': client,
        'server': ('testserver', 80),
    }
    return Request(scope)


def test_client_id_uses_x_forwarded_for_first_hop() -> None:
    request = _make_request(
        headers=[(b'x-forwarded-for', b'203.0.113.10, 10.0.0.2')],
        client=('172.18.0.1', 50000),
    )

    assert _client_id_from_request(request) == '203.0.113.10'


def test_client_id_uses_x_real_ip_when_forwarded_for_missing() -> None:
    request = _make_request(
        headers=[(b'x-real-ip', b'198.51.100.7')],
        client=('172.18.0.1', 50000),
    )

    assert _client_id_from_request(request) == '198.51.100.7'


def test_client_id_falls_back_to_request_client_host() -> None:
    request = _make_request(headers=[], client=('172.18.0.1', 50000))

    assert _client_id_from_request(request) == '172.18.0.1'


def test_client_id_unknown_when_no_headers_and_no_client() -> None:
    request = _make_request(headers=[], client=None)

    assert _client_id_from_request(request) == 'unknown'
