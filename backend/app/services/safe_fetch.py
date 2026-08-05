"""SSRF-safe HTTP(S) fetcher.

Used anywhere the backend fetches a URL supplied (directly or indirectly) by
an admin/user rather than hardcoded in config: OIDC discovery + the
`POST /auth/admin/providers/{id}/test` probe today, the Confluence importer
(Phase C) later.

Threat model: an attacker-controlled or attacker-influenced URL/hostname
must not be able to make the backend issue requests to internal
infrastructure (other pods/services, the cloud metadata endpoint, redis,
postgres, etc). Two things are required to actually close that off, not
just one:

1. Reject hostnames that resolve to a private/loopback/link-local/reserved
   address -- checked here.
2. Connect to the exact IP that was validated, not re-resolve the hostname
   when opening the socket. Otherwise a DNS-rebinding attacker returns a
   public IP to the validation check and a private one moments later to the
   actual connection (TOCTOU). Every request in this module is pinned to
   the validated IP via a custom http.client connection whose `connect()`
   is overridden; the original hostname is still sent as the `Host` header
   and TLS SNI/verification target so vhost routing and certificate checks
   still work normally.

Redirects are followed manually (stdlib http.client does not follow them)
so each hop gets the same scheme + DNS + IP validation as the original
request -- a 302 to a private address is exactly as dangerous as the
original URL pointing there.

Private targets can be selectively permitted via `allowed_private_hosts`
(admin-managed, e.g. an internal Confluence for the importer). The grant is
per-host, evaluated for EVERY redirect hop against that hop's hostname
(and, for `host:port` entries, its effective port) -- an allowlisted host
that redirects to a non-allowlisted private address is rejected at that
hop. Cloud-metadata addresses are blocked unconditionally; no allowlist
entry can override that.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

_ALLOWED_SCHEMES = {'http', 'https'}
_DEFAULT_PORTS = {'http': 80, 'https': 443}

# Caller-supplied credential headers that must never travel to a different
# origin than the one the caller addressed (see _strips_credentials).
_SENSITIVE_HEADERS = frozenset({'authorization', 'proxy-authorization', 'cookie'})

# Cloud-metadata endpoints (AWS/GCP/Azure link-local, AWS IPv6, Alibaba).
# Blocked unconditionally -- the private-host allowlist never applies here,
# even if one of these literals is added to it as an entry.
_METADATA_NETWORKS = (
    ipaddress.ip_network('169.254.169.254/32'),
    ipaddress.ip_network('fd00:ec2::254/128'),
    ipaddress.ip_network('100.100.100.200/32'),
)


class SafeFetchError(Exception):
    """Raised for anything that makes a URL unsafe or unfetchable: bad
    scheme, DNS resolution failure, a blocked/private target address, too
    many redirects, a response over the size cap, or a network error."""


@dataclass
class SafeFetchResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    final_url: str


def _canonical_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) must be evaluated against the
    # embedded IPv4 address, not as an opaque IPv6 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_metadata_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if `ip` is a cloud-metadata address. Checked before (and
    independently of) the private-host allowlist -- there is no override."""
    ip = _canonical_ip(ip)
    # ip_network.__contains__ returns False on version mismatch, so mixing
    # the IPv4 and IPv6 networks in one loop is safe.
    return any(ip in network for network in _METADATA_NETWORKS)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if `ip` is anything other than an ordinary public unicast
    address: loopback (127/8, ::1), link-local (169.254/16 and fe80::/10),
    private-use (10/8, 172.16/12, 192.168/16), unique-local IPv6
    (fc00::/7), multicast, reserved, or unspecified (0.0.0.0, ::).
    Metadata addresses are handled separately by `_is_metadata_ip`, which
    runs first and is never allowlist-exempted.
    """
    ip = _canonical_ip(ip)
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _parse_allowlist_entry(entry: str) -> tuple[str, int | None] | None:
    """Parse an allowlist entry into (hostname, port|None). Accepts
    `host`, `host:port`, `[ipv6]`, `[ipv6]:port`, and a bare IPv6 literal
    (treated as host-only). Returns None for malformed entries -- a bad
    entry must never widen the allowlist."""
    entry = entry.strip().lower()
    if not entry:
        return None
    if entry.startswith('['):
        host, bracket, rest = entry[1:].partition(']')
        if not bracket or not host:
            return None
        if not rest:
            return (host, None)
        if rest.startswith(':') and rest[1:].isdigit():
            return (host, int(rest[1:]))
        return None
    if entry.count(':') == 1:
        host, _, port_str = entry.partition(':')
        if host and port_str.isdigit():
            return (host, int(port_str))
        return None
    if ':' in entry:
        # Unbracketed IPv6 literal -- the colons are part of the address.
        return (entry, None)
    return (entry, None)


def _credential_origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or '').lower(), parts.port or _DEFAULT_PORTS.get(scheme)


def _strips_credentials(
    origin: tuple[str, str, int | None], next_origin: tuple[str, str, int | None]
) -> bool:
    """True when a redirect hop to `next_origin` must not carry the caller's
    credential headers: any host change, any https->http downgrade, any port
    change. The single exception (mirroring requests/httpx semantics) is a
    clean same-host http->https upgrade on the default ports."""
    scheme, host, port = origin
    next_scheme, next_host, next_port = next_origin
    if host != next_host:
        return True
    if scheme == next_scheme and port == next_port:
        return False
    if scheme == 'http' and next_scheme == 'https' and port == 80 and next_port == 443:
        return False
    return True


def _matches_private_host_allowlist(
    hostname: str,
    port: int | None,
    allowed_private_hosts: frozenset[str],
) -> bool:
    """True if `hostname` matches an allowlist entry. Exact case-insensitive
    hostname comparison only -- no wildcards, no suffix matching. A bare
    `host` entry matches any port; a `host:port` entry requires that exact
    effective port."""
    host = hostname.lower()
    for raw_entry in allowed_private_hosts:
        parsed = _parse_allowlist_entry(raw_entry)
        if parsed is None:
            continue
        entry_host, entry_port = parsed
        if entry_host == host and (entry_port is None or entry_port == port):
            return True
    return False


def resolve_and_pin(
    hostname: str,
    *,
    port: int | None = None,
    allowed_private_hosts: frozenset[str] | None = None,
) -> str:
    """Resolve `hostname` via DNS and return a single validated IP to
    connect to.

    Rejects the hostname outright if ANY resolved address is blocked, not
    just the first -- a host with mixed public/private A/AAAA records could
    otherwise pass validation on one lookup and connect via another.

    `allowed_private_hosts` (`host` or `host:port` entries, matched against
    `hostname` and the effective `port`) exempts this host from the
    private/loopback/link-local/ULA block. Cloud-metadata addresses are
    rejected before the allowlist is consulted and cannot be exempted.
    """
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SafeFetchError(f'DNS resolution failed for {hostname!r}: {exc}') from exc

    resolved_ips: list[str] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0].split('%', 1)[0]  # strip IPv6 zone id, if any
        if ip_str in seen:
            continue
        seen.add(ip_str)
        resolved_ips.append(ip_str)

    if not resolved_ips:
        raise SafeFetchError(f'DNS resolution for {hostname!r} returned no addresses')

    allowlisted = _matches_private_host_allowlist(hostname, port, allowed_private_hosts or frozenset())

    for ip_str in resolved_ips:
        ip = ipaddress.ip_address(ip_str)
        if _is_metadata_ip(ip):
            raise SafeFetchError(f'{hostname!r} resolves to a cloud metadata address ({ip_str})')
        if _is_blocked_ip(ip) and not allowlisted:
            raise SafeFetchError(f'{hostname!r} resolves to a blocked address ({ip_str})')

    return resolved_ips[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated IP while keeping the
    original hostname for the Host header (set by the caller)."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """As above, but wraps the pinned-IP socket in TLS with SNI/certificate
    verification against the original hostname, so a validated-but-spoofed
    IP still can't complete a handshake for someone else's certificate."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


def _single_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    body: bytes | None = None,
    allowed_private_hosts: frozenset[str] = frozenset(),
) -> tuple[int, dict[str, str], bytes, str | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SafeFetchError(f'unsupported URL scheme {parts.scheme!r} (only http/https allowed)')
    if not parts.hostname:
        raise SafeFetchError(f'URL has no hostname: {url!r}')

    hostname = parts.hostname
    port = parts.port or _DEFAULT_PORTS[scheme]
    # Allowlist evaluated here, per hop, against THIS hop's hostname:port --
    # a redirect off an allowlisted host gets no inherited exemption.
    pinned_ip = resolve_and_pin(hostname, port=port, allowed_private_hosts=allowed_private_hosts)

    path = parts.path or '/'
    if parts.query:
        path = f'{path}?{parts.query}'

    request_headers = dict(headers)
    request_headers.setdefault('Host', hostname if port == _DEFAULT_PORTS[scheme] else f'{hostname}:{port}')
    request_headers.setdefault('Accept-Encoding', 'identity')
    request_headers.setdefault('Connection', 'close')

    conn: http.client.HTTPConnection
    if scheme == 'https':
        context = ssl.create_default_context()
        conn = _PinnedHTTPSConnection(hostname, pinned_ip, port, timeout, context)
    else:
        conn = _PinnedHTTPConnection(hostname, pinned_ip, port, timeout)

    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        status_code = response.status
        response_headers = {k: v for k, v in response.getheaders()}
        response_body = response.read(max_bytes + 1)
        if len(response_body) > max_bytes:
            raise SafeFetchError(f'response body exceeds {max_bytes} byte cap')
        location = response_headers.get('Location') or response_headers.get('location')
    except (OSError, http.client.HTTPException) as exc:
        raise SafeFetchError(f'request to {url!r} failed: {exc}') from exc
    finally:
        conn.close()

    return status_code, response_headers, response_body, location


def safe_fetch(
    url: str,
    *,
    method: str = 'GET',
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
    max_redirects: int = 5,
    max_bytes: int = 2 * 1024 * 1024,
    allowed_private_hosts: frozenset[str] | None = None,
) -> SafeFetchResponse:
    """Fetch `url`, following redirects manually (each hop re-validated),
    with total size and per-request timeout caps.

    `body` (e.g. a form-encoded OAuth token exchange payload) is resent
    unchanged on 307/308 redirects and dropped when a 303 downgrades the
    request to GET, per RFC 7231 redirect semantics.

    `allowed_private_hosts` (`host` or `host:port` entries) exempts matching
    hosts from the private-address block, re-evaluated for every redirect
    hop; cloud-metadata addresses stay blocked regardless. Default
    None/empty keeps today's public-internet-only behavior.

    Raises SafeFetchError for anything unsafe or over-cap; never silently
    truncates or downgrades a rejection into a partial success.
    """
    current_url = url
    request_headers = dict(headers or {})
    request_body = body
    private_hosts = allowed_private_hosts or frozenset()
    # Credential containment across redirects: caller-supplied Authorization/
    # Cookie headers are only valid for the origin the caller addressed. Any
    # hop off that origin (other host, other port, https->http downgrade)
    # drops them permanently for the rest of the chain -- otherwise a hostile
    # or compromised server could bounce the credential to a third party (or
    # onto cleartext http) via a 3xx Location.
    origin = _credential_origin(url)

    for _hop in range(max_redirects + 1):
        status_code, response_headers, response_body, location = _single_request(
            current_url,
            method=method,
            headers=request_headers,
            timeout=timeout,
            max_bytes=max_bytes,
            body=request_body,
            allowed_private_hosts=private_hosts,
        )

        if status_code in (301, 302, 303, 307, 308) and location:
            current_url = urljoin(current_url, location)
            if status_code == 303:
                method = 'GET'
                request_body = None
            if _strips_credentials(origin, _credential_origin(current_url)):
                request_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.lower() not in _SENSITIVE_HEADERS
                }
            continue

        return SafeFetchResponse(
            status_code=status_code,
            headers=response_headers,
            body=response_body,
            final_url=current_url,
        )

    raise SafeFetchError(f'exceeded max_redirects={max_redirects} fetching {url!r}')
