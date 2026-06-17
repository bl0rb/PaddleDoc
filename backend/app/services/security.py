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


def enforce_rate_limit(request: Request) -> None:
    client_id = request.client.host if request.client else 'unknown'
    rate_limiter.check(client_id)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
