import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
import bcrypt

from app.core.config import settings


class SimpleRateLimiter:
    def __init__(self) -> None:
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_id: str) -> None:
        now = time.time()
        window_start = now - 60
        bucket = self.requests[client_id]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_per_minute:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Rate limit exceeded')
        bucket.append(now)


rate_limiter = SimpleRateLimiter()


def _client_id_from_request(request: Request) -> str:
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        first_hop = forwarded_for.split(',')[0].strip()
        if first_hop:
            return first_hop

    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        real_ip = real_ip.strip()
        if real_ip:
            return real_ip

    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


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
