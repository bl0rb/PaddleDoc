import hashlib

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import settings
from app.services.security import (
    _client_id_from_request,
    decrypt_client_secret,
    encrypt_client_secret,
    generate_session_token,
    hash_password,
    hash_session_token,
    rate_limiter,
    sign_value,
    unsign_value,
    verify_password,
)


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


def test_client_id_ignores_x_forwarded_for_from_untrusted_peer() -> None:
    # settings.trusted_proxy_ips is empty by default, so a direct client
    # (or an attacker connecting straight to us) cannot pick its own
    # rate-limit bucket by sending an arbitrary X-Forwarded-For value.
    request = _make_request(
        headers=[(b'x-forwarded-for', b'203.0.113.10, 10.0.0.2')],
        client=('172.18.0.1', 50000),
    )

    assert _client_id_from_request(request) == '172.18.0.1'


def test_client_id_ignores_x_real_ip_from_untrusted_peer() -> None:
    request = _make_request(
        headers=[(b'x-real-ip', b'198.51.100.7')],
        client=('172.18.0.1', 50000),
    )

    assert _client_id_from_request(request) == '172.18.0.1'


def test_client_id_falls_back_to_request_client_host() -> None:
    request = _make_request(headers=[], client=('172.18.0.1', 50000))

    assert _client_id_from_request(request) == '172.18.0.1'


def test_client_id_unknown_when_no_headers_and_no_client() -> None:
    request = _make_request(headers=[], client=None)

    assert _client_id_from_request(request) == 'unknown'


# --- client id: trusted-proxy X-Forwarded-For/X-Real-IP handling --------
#
# Regression coverage for the fix to _client_id_from_request: those headers
# are attacker-controlled unless the direct TCP peer is itself a configured
# trusted proxy, and even then a real proxy chain APPENDS to the header
# rather than replacing it, so the trustworthy value sits `trusted_proxy_hops`
# positions in from the right -- never the naive first (leftmost) entry.


@pytest.fixture
def _trust_single_proxy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, 'trusted_proxy_ips', ['10.0.0.5'])
    monkeypatch.setattr(settings, 'trusted_proxy_hops', 1)


def test_is_https_request_ignores_forwarded_proto_from_untrusted_peer() -> None:
    # settings.trusted_proxy_ips empty by default: a direct client that sets
    # X-Forwarded-Proto: https must NOT make us treat the request as HTTPS
    # (else it could force the session cookie Secure flag over plain HTTP).
    from app.api.auth import _is_https_request

    request = _make_request(
        headers=[(b'x-forwarded-proto', b'https')],
        client=('203.0.113.10', 50000),
    )

    assert _is_https_request(request) is False


def test_is_https_request_trusts_forwarded_proto_from_trusted_peer(_trust_single_proxy) -> None:
    from app.api.auth import _is_https_request

    request = _make_request(
        headers=[(b'x-forwarded-proto', b'https')],
        client=('10.0.0.5', 50000),
    )

    assert _is_https_request(request) is True


def test_client_id_uses_last_forwarded_for_hop_when_peer_is_trusted(_trust_single_proxy) -> None:
    # A single trusted proxy in front of us appends the one peer IP it
    # observed; that's the last (rightmost) entry, not the first.
    request = _make_request(
        headers=[(b'x-forwarded-for', b'203.0.113.10')],
        client=('10.0.0.5', 50000),
    )

    assert _client_id_from_request(request) == '203.0.113.10'


def test_client_id_ignores_attacker_prepended_hops_when_peer_is_trusted(_trust_single_proxy) -> None:
    # An attacker connecting directly to the trusted proxy can still send
    # whatever X-Forwarded-For value they like; the proxy appends the
    # attacker's real (unspoofable at the TCP layer) peer IP as the last
    # hop. We must take that last hop, not the attacker-supplied prefix.
    request = _make_request(
        headers=[(b'x-forwarded-for', b'evil-spoofed-value, 6.6.6.6')],
        client=('10.0.0.5', 50000),
    )

    assert _client_id_from_request(request) == '6.6.6.6'


def test_client_id_uses_x_real_ip_when_peer_trusted_and_forwarded_for_missing(_trust_single_proxy) -> None:
    request = _make_request(
        headers=[(b'x-real-ip', b'198.51.100.7')],
        client=('10.0.0.5', 50000),
    )

    assert _client_id_from_request(request) == '198.51.100.7'


def test_client_id_supports_cidr_trusted_proxy_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'trusted_proxy_ips', ['10.0.0.0/8'])
    monkeypatch.setattr(settings, 'trusted_proxy_hops', 1)
    request = _make_request(
        headers=[(b'x-forwarded-for', b'203.0.113.10')],
        client=('10.1.2.3', 50000),
    )

    assert _client_id_from_request(request) == '203.0.113.10'


def test_client_id_falls_back_to_peer_when_trusted_but_header_missing(_trust_single_proxy) -> None:
    request = _make_request(headers=[], client=('10.0.0.5', 50000))

    assert _client_id_from_request(request) == '10.0.0.5'


def test_hash_and_verify_password_round_trip() -> None:
    password_hash = hash_password('correct horse battery staple')

    assert verify_password('correct horse battery staple', password_hash) is True
    assert verify_password('wrong password', password_hash) is False


def test_hash_and_verify_password_over_72_bytes_does_not_raise() -> None:
    long_password = 'a' * 100

    password_hash = hash_password(long_password)

    assert verify_password(long_password, password_hash) is True


def test_verify_password_over_72_bytes_rejects_mismatch_within_first_72_bytes() -> None:
    long_password = 'a' * 72 + 'b' * 28
    other_password = 'a' * 71 + 'x' + 'b' * 28

    password_hash = hash_password(long_password)

    assert verify_password(other_password, password_hash) is False


# --- session tokens ----------------------------------------------------


def test_generate_session_token_is_random_and_url_safe() -> None:
    first = generate_session_token()
    second = generate_session_token()

    assert first != second
    assert len(first) >= 32
    # Cookie-safe: no characters that would need percent-encoding.
    assert all(c.isalnum() or c in '-_' for c in first)


def test_hash_session_token_is_deterministic_sha256_hex() -> None:
    token = 'example-token-value'

    digest = hash_session_token(token)

    assert digest == hashlib.sha256(token.encode('utf-8')).hexdigest()
    assert hash_session_token(token) == digest


def test_hash_session_token_differs_for_different_tokens() -> None:
    assert hash_session_token('token-a') != hash_session_token('token-b')


# --- OIDC client secret encryption --------------------------------------


def test_encrypt_decrypt_client_secret_round_trip() -> None:
    plaintext = 'super-secret-oidc-client-secret'

    ciphertext = encrypt_client_secret(plaintext)

    assert ciphertext != plaintext
    assert decrypt_client_secret(ciphertext) == plaintext


def test_encrypt_client_secret_is_not_deterministic() -> None:
    # Fernet includes a random IV/nonce per encryption, so two encryptions
    # of the same plaintext must not produce identical ciphertext.
    plaintext = 'same-secret'

    assert encrypt_client_secret(plaintext) != encrypt_client_secret(plaintext)


def test_decrypt_client_secret_rejects_tampered_ciphertext() -> None:
    ciphertext = encrypt_client_secret('another-secret')
    tampered = ciphertext[:-4] + ('AAAA' if ciphertext[-4:] != 'AAAA' else 'BBBB')

    with pytest.raises(ValueError):
        decrypt_client_secret(tampered)


def test_decrypt_client_secret_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    ciphertext = encrypt_client_secret('rotate-me')

    monkeypatch.setattr(settings, 'secret_key', settings.secret_key + '-rotated')

    with pytest.raises(ValueError):
        decrypt_client_secret(ciphertext)


# --- signed short-lived cookie values (OIDC state) ----------------------


def test_sign_and_unsign_value_round_trip() -> None:
    signed = sign_value('state-abc123')

    assert unsign_value(signed) == 'state-abc123'


def test_unsign_value_rejects_tampered_payload() -> None:
    signed = sign_value('state-abc123')
    value, _, mac = signed.rpartition('.')
    tampered = f'state-different.{mac}'

    assert unsign_value(tampered) is None


def test_unsign_value_rejects_tampered_signature() -> None:
    signed = sign_value('state-abc123')
    tampered = signed[:-1] + ('0' if signed[-1] != '0' else '1')

    assert unsign_value(tampered) is None


def test_unsign_value_rejects_missing_signature() -> None:
    assert unsign_value('no-dot-here') is None
    assert unsign_value('') is None


# --- rate limiting (Redis-backed, Step 4 ride-along) ---------------------
#
# Backed by a fake in-process Redis (tests/conftest.py) rather than a
# per-pod dict, so these tests also double as regression coverage for "the
# limiter still enforces settings.rate_limit_per_minute" after the Step 4
# swap from the old in-memory SimpleRateLimiter to RedisRateLimiter.


@pytest.fixture(autouse=True)
def _reset_rate_limiter_around_test():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def test_rate_limiter_allows_up_to_the_configured_limit() -> None:
    client_id = 'rate-limit-test-client-a'

    for _ in range(settings.rate_limit_per_minute):
        rate_limiter.check(client_id)  # must not raise


def test_rate_limiter_rejects_once_over_the_limit() -> None:
    client_id = 'rate-limit-test-client-b'

    for _ in range(settings.rate_limit_per_minute):
        rate_limiter.check(client_id)

    with pytest.raises(HTTPException) as exc_info:
        rate_limiter.check(client_id)
    assert exc_info.value.status_code == 429


def test_rate_limiter_tracks_clients_independently() -> None:
    saturated_client = 'rate-limit-test-client-c'
    fresh_client = 'rate-limit-test-client-d'

    for _ in range(settings.rate_limit_per_minute):
        rate_limiter.check(saturated_client)
    with pytest.raises(HTTPException):
        rate_limiter.check(saturated_client)

    # A different client_id (i.e. a different caller) must not be blocked by
    # someone else's counter -- this is the "shared Redis counter across
    # replicas" property the Step 4 swap exists for: it must key on the
    # caller, not accidentally collapse everyone into one bucket.
    rate_limiter.check(fresh_client)  # must not raise


def test_rate_limiter_reset_clears_counters() -> None:
    client_id = 'rate-limit-test-client-e'

    for _ in range(settings.rate_limit_per_minute):
        rate_limiter.check(client_id)
    with pytest.raises(HTTPException):
        rate_limiter.check(client_id)

    rate_limiter.reset()

    rate_limiter.check(client_id)  # must not raise -- counter was cleared
