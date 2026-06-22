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
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
