import base64
import hashlib
import hmac
import ipaddress
import logging
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request, status
import bcrypt
import redis as redis_lib

from app.core.config import settings

logger = logging.getLogger(__name__)


# --- Rate limiting -----------------------------------------------------------
#
# Backed by Redis (fixed 60s window via INCR/EXPIRE) rather than an
# in-process dict: the old SimpleRateLimiter's bucket lived per-pod, so with
# N replicas behind a load balancer a client could make up to
# N * rate_limit_per_minute requests/minute, and every counter silently
# reset on each pod restart/deploy. A shared Redis counter closes both gaps.

_RATE_LIMIT_KEY_PREFIX = 'ratelimit:'
_RATE_LIMIT_WINDOW_SECONDS = 60

_redis_client: 'redis_lib.Redis | None' = None


def _rate_limit_redis() -> 'redis_lib.Redis':
    """Lazily-constructed, process-wide client for rate-limit counters.

    A module-level singleton (rather than one per request) so the
    connection pool is reused. Tests replace `app.services.security._redis_client`
    directly with an in-process fake rather than talking to a real server
    (see tests/conftest.py).
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


class RedisRateLimiter:
    """Fixed-window (60s) rate limiter backed by Redis INCR/EXPIRE."""

    def check(self, client_id: str) -> None:
        key = f'{_RATE_LIMIT_KEY_PREFIX}{client_id}'
        client = _rate_limit_redis()
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
        except redis_lib.RedisError:
            # Rate limiting is defense-in-depth, not the primary control --
            # auth endpoints still require correct credentials / a valid
            # session either way. Fail open rather than 500ing every
            # request during a Redis blip; Celery (same Redis instance) is
            # already degraded at that point regardless.
            logger.warning('rate limiter: Redis unavailable, failing open', exc_info=True)
            return
        if count > settings.rate_limit_per_minute:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Rate limit exceeded')

    def reset(self) -> None:
        """Test-only: clear every rate-limit counter."""
        client = _rate_limit_redis()
        try:
            keys = list(client.keys(f'{_RATE_LIMIT_KEY_PREFIX}*'))
            if keys:
                client.delete(*keys)
        except redis_lib.RedisError:
            pass


rate_limiter = RedisRateLimiter()


def _is_trusted_proxy_peer(peer_ip: str | None) -> bool:
    """Is `peer_ip` (the immediate TCP peer) one of our own reverse proxies?

    Only when this is true is it safe to read X-Forwarded-For/X-Real-IP at
    all -- those headers are otherwise entirely attacker-controlled (any
    client can set them to whatever it likes on a direct connection).
    `settings.trusted_proxy_ips` accepts individual IPs and/or CIDR ranges
    (e.g. the in-cluster pod CIDR an ingress-controller/LB connects from).
    """
    if not peer_ip or not settings.trusted_proxy_ips:
        return False
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for entry in settings.trusted_proxy_ips:
        try:
            if '/' in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


# Public alias: other modules (e.g. the session-cookie Secure decision in
# app/api/auth.py) also need the trusted-proxy check before honouring any
# X-Forwarded-* header.
is_trusted_proxy_peer = _is_trusted_proxy_peer


def _client_id_from_request(request: Request) -> str:
    """Best-effort per-client key for the rate limiter.

    X-Forwarded-For/X-Real-IP are only trusted when the direct TCP peer is
    itself a configured, trusted reverse proxy (see
    `_is_trusted_proxy_peer`) -- otherwise any client could pick a fresh,
    self-chosen value on every request (`X-Forwarded-For: 1.2.3.4`, then
    `2.3.4.5`, ...) and get a brand new rate-limit bucket each time,
    defeating the limiter entirely. Real reverse proxies (nginx-ingress,
    ALB) APPEND their hop rather than replacing the header, so even when
    the peer is trusted we must not naively take the first (leftmost,
    still attacker-supplied) entry -- we take the hop `trusted_proxy_hops`
    positions in from the right, i.e. the value the nearest trusted proxy
    itself observed as its peer.
    """
    peer_ip = request.client.host if request.client and request.client.host else None

    if _is_trusted_proxy_peer(peer_ip):
        forwarded_for = request.headers.get('x-forwarded-for')
        if forwarded_for:
            hops = [hop.strip() for hop in forwarded_for.split(',') if hop.strip()]
            if hops:
                index = len(hops) - settings.trusted_proxy_hops
                if 0 <= index < len(hops):
                    return hops[index]
                # Fewer hops present than the configured trusted-proxy
                # count -- don't guess past the start of the chain.
                return hops[0]

        real_ip = request.headers.get('x-real-ip')
        if real_ip and real_ip.strip():
            return real_ip.strip()

    return peer_ip or 'unknown'


def enforce_rate_limit(request: Request) -> None:
    client_id = _client_id_from_request(request)
    rate_limiter.check(client_id)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    # bcrypt only considers the first 72 bytes of the input. Versions < 5.0
    # silently truncated; 5.0+ raises ValueError instead, so truncate here
    # ourselves to preserve the pre-5.0 behavior.
    password_bytes = password.encode('utf-8')[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    # See hash_password: truncate to bcrypt's 72-byte limit so long
    # passwords hashed under bcrypt < 5.0 remain verifiable.
    password_bytes = password.encode('utf-8')[:72]
    return bcrypt.checkpw(password_bytes, password_hash.encode('utf-8'))


# --- Session tokens ---------------------------------------------------------
#
# Opaque, DB-backed session cookie (not a JWT): the raw token only ever lives
# in the client's cookie and this function's return value; the DB stores
# sha256(token) in sessions.token_hash. A leaked DB row alone can't be
# replayed as a cookie, and revocation (logout / is_active=false / user
# delete) takes effect instantly since every lookup hits the DB.

def generate_session_token() -> str:
    """Generate a new opaque bearer token for the session cookie."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """sha256 hex digest of a session token, as stored in sessions.token_hash."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# --- OIDC client secret encryption -----------------------------------------
#
# auth_providers.client_secret_encrypted is Fernet-encrypted at rest. The
# Fernet key is not stored anywhere -- it's derived on demand from
# SECRET_KEY via HKDF-SHA256 with a fixed, purpose-specific `info` label, so
# rotating SECRET_KEY (intentionally or not) also invalidates every stored
# client secret rather than silently decrypting with the wrong key.

_OIDC_CLIENT_SECRET_HKDF_INFO = b'oidc-client-secret'
_IMPORT_CREDENTIAL_HKDF_INFO = b'import-source-credential'
_VL_CONNECTION_API_KEY_HKDF_INFO = b'vl-connection-api-key'


def _derive_fernet_key(info: bytes) -> bytes:
    """Derive a purpose-bound Fernet key from SECRET_KEY. The `info` label
    separates key domains: an OIDC-secret key can never decrypt an import
    credential and vice versa."""
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(settings.secret_key.encode('utf-8'))
    return base64.urlsafe_b64encode(key_material)


def _oidc_client_secret_fernet_key() -> bytes:
    return _derive_fernet_key(_OIDC_CLIENT_SECRET_HKDF_INFO)


def encrypt_client_secret(plaintext: str) -> str:
    """Fernet-encrypt an OIDC client secret for storage in
    auth_providers.client_secret_encrypted."""
    fernet = Fernet(_oidc_client_secret_fernet_key())
    return fernet.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_client_secret(ciphertext: str) -> str:
    """Inverse of encrypt_client_secret.

    Raises ValueError if the ciphertext is malformed/tampered, or was
    encrypted under a different SECRET_KEY (e.g. after a key rotation).
    """
    fernet = Fernet(_oidc_client_secret_fernet_key())
    try:
        return fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError('client secret could not be decrypted (wrong SECRET_KEY or corrupted value)') from exc


# --- Confluence-import source credential encryption -------------------------
#
# Same Fernet-over-HKDF pattern as OIDC client secrets, under its own info
# label. import_sources.credential_encrypted is write-only at the API; the
# plaintext exists only inside the /test endpoint and the import worker task.

def encrypt_import_credential(plaintext: str) -> str:
    """Fernet-encrypt an import-source credential (Cloud API token or
    Server/DC PAT) for storage in import_sources.credential_encrypted."""
    fernet = Fernet(_derive_fernet_key(_IMPORT_CREDENTIAL_HKDF_INFO))
    return fernet.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_import_credential(ciphertext: str) -> str:
    """Inverse of encrypt_import_credential.

    Raises ValueError if the ciphertext is malformed/tampered, or was
    encrypted under a different SECRET_KEY (e.g. after a key rotation).
    """
    fernet = Fernet(_derive_fernet_key(_IMPORT_CREDENTIAL_HKDF_INFO))
    try:
        return fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError('import credential could not be decrypted (wrong SECRET_KEY or corrupted value)') from exc


# --- VL connection API key encryption ----------------------------------------
#
# Same Fernet-over-HKDF pattern as OIDC client secrets / import credentials,
# under its own info label. vl_connections.api_key_encrypted is write-only at
# the API; the plaintext exists only inside the admin /test endpoint and the
# worker's benchmark job dispatch (see app/workers/tasks.py).

def encrypt_vl_api_key(plaintext: str) -> str:
    """Fernet-encrypt a VL connection API key for storage in
    vl_connections.api_key_encrypted."""
    fernet = Fernet(_derive_fernet_key(_VL_CONNECTION_API_KEY_HKDF_INFO))
    return fernet.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_vl_api_key(ciphertext: str) -> str:
    """Inverse of encrypt_vl_api_key.

    Raises ValueError if the ciphertext is malformed/tampered, or was
    encrypted under a different SECRET_KEY (e.g. after a key rotation).
    """
    fernet = Fernet(_derive_fernet_key(_VL_CONNECTION_API_KEY_HKDF_INFO))
    try:
        return fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError('VL connection API key could not be decrypted (wrong SECRET_KEY or corrupted value)') from exc


# --- Short-lived signed cookie values (OIDC state/nonce/PKCE) --------------
#
# Plain HMAC-SHA256 rather than a JWT/itsdangerous dependency: stateless
# (works across replicas with no shared cache) and cheap since these cookies
# are opaque and short-lived (~10min), not bearer credentials in their own
# right -- unlike the session token above, there is nothing to revoke.

def sign_value(value: str) -> str:
    """HMAC-sign an opaque string as "<value>.<hex-hmac-sha256>"."""
    mac = hmac.new(settings.secret_key.encode('utf-8'), value.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'{value}.{mac}'


def unsign_value(signed_value: str) -> str | None:
    """Verify a value produced by sign_value with a constant-time compare.

    Returns the original value, or None if the signature is missing,
    malformed, or does not match.
    """
    value, separator, mac = signed_value.rpartition('.')
    if not separator or not value or not mac:
        return None
    expected = hmac.new(settings.secret_key.encode('utf-8'), value.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    return value
