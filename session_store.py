"""Bounded RAM only; no transcripts are written to disk or logs."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import time


@dataclass
class Session:
    token: str
    last_seen: float
    turns: list[list[dict]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    name: str = ''
    phone: str = ''
    callback_consent: bool = False
    pending: bool = False
    abandoned_notified: bool = False
    callback_attempt: float = 0
    callback_expired: bool = False
    generation: int = 0
    total_turns: int = 0
    callback_boundary: int = 0
    responses: dict[str, tuple[str, dict]] = field(default_factory=dict)

    def clear_contact(self):
        self.name = self.phone = ''
        self.pending = self.callback_consent = False
        self.callback_boundary = self.total_turns


class SessionStore:
    def __init__(self, settings, now=time.monotonic):
        self.settings = settings
        self.now = now
        self.sessions: dict[str, Session] = {}

    def get(self, token: str | None):
        session = self.sessions.get(token or '')
        if session and self.now() - session.last_seen < self.settings.session_seconds:
            return session
        return None

    def create(self):
        if len(self.sessions) >= self.settings.max_sessions:
            raise OverflowError('Session capacity reached')
        session = Session(secrets.token_urlsafe(32), self.now())
        self.sessions[session.token] = session
        return session

    def discard(self, token):
        self.sessions.pop(token, None)


class RateLimiter:
    def __init__(self, secret: str, now=time.monotonic):
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.now = now
        self.buckets: dict[str, deque] = {}

    def allow(self, identity: str, scope: str, maximum: int, seconds: int = 60):
        key = hmac.new(self.secret, f'{scope}:{identity}'.encode(), hashlib.sha256).hexdigest()
        now = self.now()
        bucket = self.buckets.setdefault(key, deque())
        while bucket and bucket[0] <= now - seconds:
            bucket.popleft()
        if len(bucket) >= maximum:
            return False
        if len(self.buckets) > 10000:
            self.buckets = {k: v for k, v in self.buckets.items() if v and v[-1] > now - 600}
            if len(self.buckets) > 10000:
                return False
        bucket.append(now)
        return True
