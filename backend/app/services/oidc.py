"""OIDC discovery, token exchange, and ID token validation helpers.

Every outbound HTTP call here (discovery document, JWKS, token exchange) is
routed through app.services.safe_fetch.safe_fetch rather than a bare HTTP
client: `issuer_url` is admin-supplied, and the token/JWKS endpoints are
whatever that discovery document says -- both are exactly the kind of
attacker-influenceable URL safe_fetch exists for (SSRF via private-IP
targets or DNS rebinding), same threat model as the
`POST /auth/admin/providers/{id}/test` probe.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet

from app.services.safe_fetch import SafeFetchError, safe_fetch

# Discovery documents change rarely; caching in-process for 15min avoids a
# safe_fetch round trip (DNS + TLS handshake) on every single OIDC login
# without going stale for long. Per-process cache is fine even with
# multiple replicas -- worst case each pod re-fetches independently.
_DISCOVERY_CACHE_TTL_SECONDS = 15 * 60
_discovery_cache: dict[str, tuple[float, dict]] = {}

_SUPPORTED_ID_TOKEN_ALGORITHMS = ['RS256', 'ES256']


class OIDCError(Exception):
    """Raised for any OIDC protocol failure: unreachable discovery/token/
    JWKS endpoint, malformed response, or ID token validation failure."""


def get_discovery_document(issuer_url: str) -> dict:
    cached = _discovery_cache.get(issuer_url)
    if cached is not None:
        cached_at, document = cached
        if time.time() - cached_at < _DISCOVERY_CACHE_TTL_SECONDS:
            return document

    discovery_url = issuer_url.rstrip('/') + '/.well-known/openid-configuration'
    try:
        response = safe_fetch(discovery_url)
    except SafeFetchError as exc:
        raise OIDCError(f'OIDC discovery failed for {issuer_url!r}: {exc}') from exc
    if response.status_code != 200:
        raise OIDCError(f'OIDC discovery for {issuer_url!r} returned HTTP {response.status_code}')
    try:
        document = json.loads(response.body)
    except ValueError as exc:
        raise OIDCError(f'OIDC discovery document for {issuer_url!r} is not valid JSON') from exc

    _discovery_cache[issuer_url] = (time.time(), document)
    return document


def exchange_code_for_tokens(token_endpoint: str, **form_params: str) -> dict:
    """POST a form-encoded body to the token endpoint (authorization_code
    grant) and return the parsed JSON response."""
    body = urlencode(form_params).encode('utf-8')
    try:
        response = safe_fetch(
            token_endpoint,
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            body=body,
        )
    except SafeFetchError as exc:
        raise OIDCError(f'token exchange failed: {exc}') from exc
    if response.status_code != 200:
        raise OIDCError(f'token endpoint returned HTTP {response.status_code}: {response.body[:500]!r}')
    try:
        return json.loads(response.body)
    except ValueError as exc:
        raise OIDCError('token endpoint response is not valid JSON') from exc


def fetch_jwks(jwks_uri: str) -> KeySet:
    try:
        response = safe_fetch(jwks_uri)
    except SafeFetchError as exc:
        raise OIDCError(f'JWKS fetch failed: {exc}') from exc
    if response.status_code != 200:
        raise OIDCError(f'JWKS endpoint returned HTTP {response.status_code}')
    try:
        jwks_dict = json.loads(response.body)
    except ValueError as exc:
        raise OIDCError('JWKS response is not valid JSON') from exc
    return KeySet.import_key_set(jwks_dict)


def validate_id_token(
    id_token: str,
    *,
    key_set: KeySet,
    issuer: str,
    audience: str,
    nonce: str,
) -> dict:
    """Verify signature (against `key_set`) and standard claims (iss/aud/
    exp) via joserfc, plus the OIDC nonce (bound to our own /authorize call,
    not a registered JWT claim joserfc validates itself). Returns the claim
    set on success; raises OIDCError on any failure."""
    try:
        token = joserfc_jwt.decode(id_token, key_set, algorithms=_SUPPORTED_ID_TOKEN_ALGORITHMS)
    except Exception as exc:  # noqa: BLE001 -- any decode/signature failure is fatal here
        raise OIDCError(f'ID token signature validation failed: {exc}') from exc

    claims = token.claims
    registry = joserfc_jwt.JWTClaimsRegistry(
        iss={'essential': True, 'values': [issuer]},
        aud={'essential': True, 'values': [audience]},
        exp={'essential': True},
    )
    try:
        registry.validate(claims)
    except Exception as exc:  # noqa: BLE001 -- any claim validation failure is fatal here
        raise OIDCError(f'ID token claim validation failed: {exc}') from exc

    if claims.get('nonce') != nonce:
        raise OIDCError('ID token nonce mismatch')

    return claims
