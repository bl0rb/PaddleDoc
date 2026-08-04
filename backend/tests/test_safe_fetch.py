import socket

import pytest

from app.services.safe_fetch import SafeFetchError, resolve_and_pin


def _mock_getaddrinfo(mapping: dict[str, str]):
    """Return a fake socket.getaddrinfo that resolves hostnames per
    `mapping` (hostname -> ip string) without touching real DNS."""

    def fake(host, *args, **kwargs):
        ip = mapping[host]
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (ip, 0))]

    return fake


@pytest.mark.parametrize(
    'ip',
    [
        '127.0.0.1',  # loopback
        '10.0.0.5',  # private (RFC1918)
        '172.16.0.5',  # private (RFC1918)
        '192.168.1.5',  # private (RFC1918)
        '169.254.169.254',  # link-local / cloud metadata
        '0.0.0.0',  # unspecified
        '224.0.0.1',  # multicast
        '::1',  # loopback (IPv6)
        'fc00::1',  # unique local (IPv6 ULA)
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
