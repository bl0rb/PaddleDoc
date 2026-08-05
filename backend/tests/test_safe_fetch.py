import socket

import pytest

from app.services import safe_fetch as safe_fetch_module
from app.services.safe_fetch import SafeFetchError, resolve_and_pin, safe_fetch


def _mock_getaddrinfo(mapping: dict[str, str]):
    """Return a fake socket.getaddrinfo that resolves hostnames per
    `mapping` (hostname -> ip string) without touching real DNS."""

    def fake(host, *args, **kwargs):
        ip = mapping[host]
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (ip, 0))]

    return fake


class _FakeHTTPResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._headers = headers
        self._body = body

    def getheaders(self):
        return list(self._headers.items())

    def read(self, amt=None):
        return self._body


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, tuple[int, dict[str, str], bytes]],
    header_log: list[tuple[str, dict[str, str]]] | None = None,
) -> list[tuple[str, str]]:
    """Replace both pinned connection classes with a fake transport serving
    canned (status, headers, body) responses keyed by 'host:port/path'.
    Returns a live list of (key, pinned_ip) tuples recording every request
    actually issued -- URLs rejected during validation never appear, which
    lets tests assert a blocked hop was rejected before any connection.
    `header_log`, if given, additionally records (key, request headers) per
    hop so redirect credential-stripping can be asserted."""
    requests_made: list[tuple[str, str]] = []

    class FakeConnection:
        def __init__(self, hostname, pinned_ip, port, timeout, context=None):
            self._host = hostname
            self._port = port
            self._pinned_ip = pinned_ip
            self._key = ''

        def request(self, method, path, body=None, headers=None):
            self._key = f'{self._host}:{self._port}{path}'
            requests_made.append((self._key, self._pinned_ip))
            if header_log is not None:
                header_log.append((self._key, dict(headers or {})))

        def getresponse(self):
            return _FakeHTTPResponse(*responses[self._key])

        def close(self):
            pass

    monkeypatch.setattr(safe_fetch_module, '_PinnedHTTPConnection', FakeConnection)
    monkeypatch.setattr(safe_fetch_module, '_PinnedHTTPSConnection', FakeConnection)
    return requests_made


@pytest.mark.parametrize(
    'ip',
    [
        '127.0.0.1',  # loopback
        '10.0.0.5',  # private (RFC1918)
        '172.16.0.5',  # private (RFC1918)
        '192.168.1.5',  # private (RFC1918)
        '169.254.169.254',  # link-local / cloud metadata
        '100.100.100.200',  # Alibaba cloud metadata (CGN space, not is_private)
        '0.0.0.0',  # unspecified
        '224.0.0.1',  # multicast
        '::1',  # loopback (IPv6)
        'fc00::1',  # unique local (IPv6 ULA)
        'fd00:ec2::254',  # AWS IPv6 metadata
        'fe80::1',  # link-local (IPv6)
        '::ffff:127.0.0.1',  # IPv4-mapped loopback
    ],
)
def test_resolve_and_pin_rejects_private_and_metadata_addresses(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'evil.example': ip}))

    with pytest.raises(SafeFetchError):
        resolve_and_pin('evil.example')


@pytest.mark.parametrize('ip', ['8.8.8.8', '93.184.216.34', '2001:4860:4860::8888'])
def test_resolve_and_pin_allows_public_addresses(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'public.example': ip}))

    assert resolve_and_pin('public.example') == ip


def test_resolve_and_pin_rejects_hostname_with_any_blocked_record(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single hostname resolving to a mix of public + private addresses
    # (e.g. attacker-controlled DNS round-robin) must be rejected outright,
    # not merely have the private record silently skipped.
    def fake(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
        ]

    monkeypatch.setattr(socket, 'getaddrinfo', fake)

    with pytest.raises(SafeFetchError):
        resolve_and_pin('mixed.example')


def test_resolve_and_pin_raises_on_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host, *args, **kwargs):
        raise socket.gaierror('name resolution failed')

    monkeypatch.setattr(socket, 'getaddrinfo', fake)

    with pytest.raises(SafeFetchError):
        resolve_and_pin('does-not-resolve.example')


def test_fetch_public_host_succeeds_and_pins_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backward-compat guard: default arguments (no allowlist) keep today's
    # public-internet behavior, and the connection uses the validated IP.
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'public.example': '93.184.216.34'}))
    made = _install_fake_transport(monkeypatch, {'public.example:80/ok': (200, {}, b'hello')})

    response = safe_fetch('http://public.example/ok')

    assert response.status_code == 200
    assert response.body == b'hello'
    assert response.final_url == 'http://public.example/ok'
    assert made == [('public.example:80/ok', '93.184.216.34')]


def test_allowlisted_private_host_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5'}))
    made = _install_fake_transport(monkeypatch, {'wiki.corp.internal:80/page': (200, {}, b'wiki')})

    response = safe_fetch(
        'http://wiki.corp.internal/page',
        allowed_private_hosts=frozenset({'wiki.corp.internal'}),
    )

    assert response.status_code == 200
    assert response.body == b'wiki'
    assert made == [('wiki.corp.internal:80/page', '10.0.0.5')]


@pytest.mark.parametrize(
    'allowlist',
    [None, frozenset(), frozenset({'other.internal'}), frozenset({'corp.internal'})],
)
def test_non_allowlisted_private_host_blocked(
    monkeypatch: pytest.MonkeyPatch, allowlist: frozenset[str] | None
) -> None:
    # No entry, a different host, and a parent-domain entry (no suffix
    # matching) must all leave the private target blocked, before connect.
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5'}))
    made = _install_fake_transport(monkeypatch, {})

    with pytest.raises(SafeFetchError):
        safe_fetch('http://wiki.corp.internal/page', allowed_private_hosts=allowlist)

    assert made == []


def test_redirect_from_allowlisted_to_non_allowlisted_private_host_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The allowlist grant is per-host, per-hop: an allowlisted internal host
    # 302ing to another private host must be rejected at that hop, with no
    # connection ever made to the redirect target.
    monkeypatch.setattr(
        socket,
        'getaddrinfo',
        _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5', 'db.corp.internal': '10.0.0.10'}),
    )
    made = _install_fake_transport(
        monkeypatch,
        {'wiki.corp.internal:80/start': (302, {'Location': 'http://db.corp.internal:5432/'}, b'')},
    )

    with pytest.raises(SafeFetchError):
        safe_fetch(
            'http://wiki.corp.internal/start',
            allowed_private_hosts=frozenset({'wiki.corp.internal'}),
        )

    assert made == [('wiki.corp.internal:80/start', '10.0.0.5')]


def test_redirect_between_allowlisted_private_hosts_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        'getaddrinfo',
        _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5', 'files.corp.internal': '10.0.0.6'}),
    )
    made = _install_fake_transport(
        monkeypatch,
        {
            'wiki.corp.internal:80/start': (302, {'Location': 'http://files.corp.internal/file'}, b''),
            'files.corp.internal:80/file': (200, {}, b'bytes'),
        },
    )

    response = safe_fetch(
        'http://wiki.corp.internal/start',
        allowed_private_hosts=frozenset({'wiki.corp.internal', 'files.corp.internal'}),
    )

    assert response.status_code == 200
    assert response.body == b'bytes'
    assert response.final_url == 'http://files.corp.internal/file'
    assert [key for key, _ip in made] == ['wiki.corp.internal:80/start', 'files.corp.internal:80/file']


@pytest.mark.parametrize(
    'ip',
    [
        '169.254.169.254',  # AWS/GCP/Azure metadata
        '100.100.100.200',  # Alibaba metadata
        'fd00:ec2::254',  # AWS IPv6 metadata
        '::ffff:169.254.169.254',  # IPv4-mapped form must not slip through
    ],
)
def test_metadata_ip_blocked_even_when_allowlisted(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    # The metadata block is unconditional: neither the hostname nor the
    # metadata IP literal itself being on the allowlist can override it.
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'metadata.corp.internal': ip}))
    made = _install_fake_transport(monkeypatch, {})

    with pytest.raises(SafeFetchError):
        safe_fetch(
            'http://metadata.corp.internal/latest/meta-data/',
            allowed_private_hosts=frozenset({'metadata.corp.internal', ip, '169.254.169.254'}),
        )

    assert made == []


def test_metadata_redirect_from_allowlisted_host_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        'getaddrinfo',
        _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5', 'metadata.corp.internal': '169.254.169.254'}),
    )
    made = _install_fake_transport(
        monkeypatch,
        {'wiki.corp.internal:80/start': (302, {'Location': 'http://metadata.corp.internal/latest/'}, b'')},
    )

    with pytest.raises(SafeFetchError):
        safe_fetch(
            'http://wiki.corp.internal/start',
            allowed_private_hosts=frozenset({'wiki.corp.internal', 'metadata.corp.internal'}),
        )

    assert made == [('wiki.corp.internal:80/start', '10.0.0.5')]


def test_host_port_allowlist_entry_matches_only_that_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5'}))
    made = _install_fake_transport(monkeypatch, {'wiki.corp.internal:8090/page': (200, {}, b'ok')})
    allowlist = frozenset({'wiki.corp.internal:8090'})

    response = safe_fetch('http://wiki.corp.internal:8090/page', allowed_private_hosts=allowlist)
    assert response.status_code == 200

    with pytest.raises(SafeFetchError):
        # Same host on the default port (80) is NOT covered by a :8090 entry.
        safe_fetch('http://wiki.corp.internal/page', allowed_private_hosts=allowlist)
    with pytest.raises(SafeFetchError):
        safe_fetch('http://wiki.corp.internal:9000/page', allowed_private_hosts=allowlist)

    assert made == [('wiki.corp.internal:8090/page', '10.0.0.5')]


def test_cross_host_redirect_drops_credential_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Credential containment: a hostile/compromised server answering
    # 302 Location: <other host> must not receive the caller's Authorization
    # or Cookie on the next hop (matches requests/httpx semantics); benign
    # headers still travel.
    monkeypatch.setattr(
        socket,
        'getaddrinfo',
        _mock_getaddrinfo({'wiki.example': '93.184.216.34', 'attacker.example': '93.184.216.35'}),
    )
    header_log: list[tuple[str, dict[str, str]]] = []
    _install_fake_transport(
        monkeypatch,
        {
            'wiki.example:80/start': (302, {'Location': 'http://attacker.example/collect'}, b''),
            'attacker.example:80/collect': (200, {}, b'ok'),
        },
        header_log=header_log,
    )

    response = safe_fetch(
        'http://wiki.example/start',
        headers={'Authorization': 'Bearer secret-tok', 'Cookie': 'sid=1', 'Accept': 'application/json'},
    )

    assert response.status_code == 200
    (first_key, first_headers), (second_key, second_headers) = header_log
    assert first_key == 'wiki.example:80/start'
    assert first_headers.get('Authorization') == 'Bearer secret-tok'
    assert second_key == 'attacker.example:80/collect'
    assert 'Authorization' not in second_headers
    assert 'Cookie' not in second_headers
    assert second_headers.get('Accept') == 'application/json'


def test_same_origin_redirect_keeps_credential_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.example': '93.184.216.34'}))
    header_log: list[tuple[str, dict[str, str]]] = []
    _install_fake_transport(
        monkeypatch,
        {
            'wiki.example:80/start': (302, {'Location': 'http://wiki.example/next'}, b''),
            'wiki.example:80/next': (200, {}, b'ok'),
        },
        header_log=header_log,
    )

    response = safe_fetch('http://wiki.example/start', headers={'Authorization': 'Bearer secret-tok'})

    assert response.status_code == 200
    assert header_log[1][0] == 'wiki.example:80/next'
    assert header_log[1][1].get('Authorization') == 'Bearer secret-tok'


def test_https_to_http_downgrade_redirect_drops_credential_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same host, but the downgraded hop would resend the credential in
    # cleartext -- it must be stripped.
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.example': '93.184.216.34'}))
    header_log: list[tuple[str, dict[str, str]]] = []
    _install_fake_transport(
        monkeypatch,
        {
            'wiki.example:443/start': (302, {'Location': 'http://wiki.example/next'}, b''),
            'wiki.example:80/next': (200, {}, b'ok'),
        },
        header_log=header_log,
    )

    response = safe_fetch('https://wiki.example/start', headers={'Authorization': 'Bearer secret-tok'})

    assert response.status_code == 200
    assert header_log[1][0] == 'wiki.example:80/next'
    assert 'Authorization' not in header_log[1][1]


def test_bare_host_allowlist_entry_matches_any_port_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, 'getaddrinfo', _mock_getaddrinfo({'wiki.corp.internal': '10.0.0.5'}))
    made = _install_fake_transport(monkeypatch, {'wiki.corp.internal:8090/page': (200, {}, b'ok')})

    response = safe_fetch(
        'http://wiki.corp.internal:8090/page',
        allowed_private_hosts=frozenset({'Wiki.CORP.internal'}),
    )

    assert response.status_code == 200
    assert made == [('wiki.corp.internal:8090/page', '10.0.0.5')]
