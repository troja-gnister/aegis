from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import LoginThrottleBucket

WINDOW = timedelta(minutes=15)
BLOCK_AGE = timedelta(minutes=15)
ACCOUNT_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after_seconds: int
    bucket_type: str | None
    account_key: str


@dataclass(frozen=True)
class FailureRecord:
    audit_event_type: str | None
    audit_bucket_type: str | None


class ThrottleUnavailable(RuntimeError):
    """Raised when authentication throttling cannot be applied safely."""


def _secret() -> bytes:
    value = getattr(settings, "AEGIS_AUTH_THROTTLE_HMAC_KEY", None)
    if not isinstance(value, str):
        raise ThrottleUnavailable("authentication throttle unavailable")
    encoded = value.encode("utf-8")
    if not 32 <= len(encoded) <= 4096:
        raise ThrottleUnavailable("authentication throttle unavailable")
    return encoded


def _account_value(username: str) -> str:
    if not isinstance(username, str) or len(username) > 150:
        raise ValueError("invalid account identifier")
    return unicodedata.normalize("NFKC", username).casefold()


def _ip_value(client_ip: str) -> str:
    if not isinstance(client_ip, str) or len(client_ip) > 64:
        return "invalid-client"
    try:
        return ipaddress.ip_address(client_ip).compressed
    except ValueError:
        return "invalid-client"


def _digest(*, secret: bytes, kind: str, value: str) -> bytes:
    return hmac.new(
        secret,
        f"{kind}:{value}".encode(),
        hashlib.sha256,
    ).digest()


def _locked_bucket(*, kind: str, key_digest: bytes, now: datetime) -> LoginThrottleBucket:
    try:
        return LoginThrottleBucket.objects.select_for_update().get(
            kind=kind,
            key_digest=key_digest,
        )
    except LoginThrottleBucket.DoesNotExist:
        try:
            with transaction.atomic():
                LoginThrottleBucket.objects.create(
                    kind=kind,
                    key_digest=key_digest,
                    window_started_at=now,
                )
        except IntegrityError:
            pass
        return LoginThrottleBucket.objects.select_for_update().get(
            kind=kind,
            key_digest=key_digest,
        )


def _retry_after(*, bucket: LoginThrottleBucket | None, now: datetime) -> int:
    if bucket is None or bucket.blocked_until is None or bucket.blocked_until <= now:
        return 0
    return max(1, math.ceil((bucket.blocked_until - now).total_seconds()))


def _advisory_lock_id(*, kind: str, key_digest: bytes) -> int:
    domain = b"aegis:auth:login-throttle-lock:v1\x00"
    value = hashlib.sha256(domain + kind.encode("ascii") + b"\x00" + key_digest).digest()
    return int.from_bytes(value[:8], byteorder="big", signed=True)


def _acquire_advisory_lock(*, kind: str, key_digest: bytes) -> None:
    lock_id = _advisory_lock_id(kind=kind, key_digest=key_digest)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _decision(*, account_key: bytes, ip_key: bytes, now: datetime) -> ThrottleDecision:
    buckets = {
        bucket.kind: bucket
        for bucket in LoginThrottleBucket.objects.filter(
            kind__in=(
                LoginThrottleBucket.Kind.ACCOUNT,
                LoginThrottleBucket.Kind.IP,
            ),
            key_digest__in=(account_key, ip_key),
        )
    }
    account_retry = _retry_after(
        bucket=buckets.get(LoginThrottleBucket.Kind.ACCOUNT), now=now
    )
    ip_retry = _retry_after(bucket=buckets.get(LoginThrottleBucket.Kind.IP), now=now)
    bucket_type: str | None = None
    retry_after = 0
    if account_retry:
        bucket_type = LoginThrottleBucket.Kind.ACCOUNT
        retry_after = account_retry
    elif ip_retry:
        bucket_type = LoginThrottleBucket.Kind.IP
        retry_after = ip_retry
    return ThrottleDecision(
        allowed=retry_after == 0,
        retry_after_seconds=retry_after,
        bucket_type=bucket_type,
        account_key=account_key.hex(),
    )


def _record_failure(
    *, account_key: bytes, ip_key: bytes, now: datetime
) -> FailureRecord:
    values = (
        (LoginThrottleBucket.Kind.ACCOUNT, account_key, ACCOUNT_FAILURE_LIMIT),
        (LoginThrottleBucket.Kind.IP, ip_key, IP_FAILURE_LIMIT),
    )
    newly_blocked: str | None = None
    first_ip_failure = False
    for kind, key_digest, limit in values:
        bucket = _locked_bucket(kind=kind, key_digest=key_digest, now=now)
        if now - bucket.window_started_at >= WINDOW or (
            bucket.blocked_until is not None and bucket.blocked_until <= now
        ):
            bucket.window_started_at = now
            bucket.failures = 0
            bucket.blocked_until = None
        bucket.failures = min(limit, bucket.failures + 1)
        if bucket.failures >= limit and bucket.blocked_until is None:
            bucket.blocked_until = now + BLOCK_AGE
            newly_blocked = kind
        if kind == LoginThrottleBucket.Kind.IP and bucket.failures == 1:
            first_ip_failure = True
        bucket.save(
            update_fields=(
                "window_started_at",
                "failures",
                "blocked_until",
            )
        )
    if newly_blocked is not None:
        return FailureRecord("auth.login.throttled", newly_blocked)
    if first_ip_failure:
        return FailureRecord(
            "auth.login.failed",
            LoginThrottleBucket.Kind.ACCOUNT,
        )
    return FailureRecord(None, None)


def _record_success(*, account_key: bytes) -> None:
    bucket = (
        LoginThrottleBucket.objects.select_for_update()
        .filter(
            kind=LoginThrottleBucket.Kind.ACCOUNT,
            key_digest=account_key,
        )
        .first()
    )
    if bucket is not None:
        bucket.delete()


@dataclass
class LoginThrottleAdmission:
    decision: ThrottleDecision
    _account_key: bytes
    _ip_key: bytes
    _now: datetime

    def record_failure(self) -> FailureRecord:
        if not self.decision.allowed:
            raise RuntimeError("blocked throttle admission has no authentication outcome")
        return _record_failure(
            account_key=self._account_key,
            ip_key=self._ip_key,
            now=self._now,
        )

    def record_success(self) -> None:
        if not self.decision.allowed:
            raise RuntimeError("blocked throttle admission has no authentication outcome")
        _record_success(account_key=self._account_key)


class LoginThrottle:
    def _keys(self, *, username: str, client_ip: str) -> tuple[bytes, bytes]:
        secret = _secret()
        return (
            _digest(
                secret=secret,
                kind=LoginThrottleBucket.Kind.ACCOUNT,
                value=_account_value(username),
            ),
            _digest(
                secret=secret,
                kind=LoginThrottleBucket.Kind.IP,
                value=_ip_value(client_ip),
            ),
        )

    def check(self, *, username: str, client_ip: str) -> ThrottleDecision:
        account_key, ip_key = self._keys(username=username, client_ip=client_ip)
        with transaction.atomic():
            _acquire_advisory_lock(
                kind=LoginThrottleBucket.Kind.ACCOUNT,
                key_digest=account_key,
            )
            _acquire_advisory_lock(kind=LoginThrottleBucket.Kind.IP, key_digest=ip_key)
            return _decision(account_key=account_key, ip_key=ip_key, now=timezone.now())

    @contextmanager
    def admission(
        self, *, username: str, client_ip: str
    ) -> Iterator[LoginThrottleAdmission]:
        account_key, ip_key = self._keys(username=username, client_ip=client_ip)
        with transaction.atomic():
            _acquire_advisory_lock(
                kind=LoginThrottleBucket.Kind.ACCOUNT,
                key_digest=account_key,
            )
            _acquire_advisory_lock(kind=LoginThrottleBucket.Kind.IP, key_digest=ip_key)
            now = timezone.now()
            yield LoginThrottleAdmission(
                decision=_decision(account_key=account_key, ip_key=ip_key, now=now),
                _account_key=account_key,
                _ip_key=ip_key,
                _now=now,
            )

    def record_failure(self, *, username: str, client_ip: str) -> FailureRecord:
        account_key, ip_key = self._keys(username=username, client_ip=client_ip)
        with transaction.atomic():
            _acquire_advisory_lock(
                kind=LoginThrottleBucket.Kind.ACCOUNT,
                key_digest=account_key,
            )
            _acquire_advisory_lock(kind=LoginThrottleBucket.Kind.IP, key_digest=ip_key)
            return _record_failure(
                account_key=account_key,
                ip_key=ip_key,
                now=timezone.now(),
            )

    def record_success(self, *, username: str) -> None:
        account_key = _digest(
            secret=_secret(),
            kind=LoginThrottleBucket.Kind.ACCOUNT,
            value=_account_value(username),
        )
        with transaction.atomic():
            _acquire_advisory_lock(
                kind=LoginThrottleBucket.Kind.ACCOUNT,
                key_digest=account_key,
            )
            _record_success(account_key=account_key)
