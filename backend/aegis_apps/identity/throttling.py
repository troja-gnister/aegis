from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
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


def _secret() -> bytes:
    value = getattr(settings, "AEGIS_AUTH_THROTTLE_HMAC_KEY", None)
    if not isinstance(value, str):
        raise RuntimeError("authentication throttle unavailable")
    encoded = value.encode("utf-8")
    if not 32 <= len(encoded) <= 4096:
        raise RuntimeError("authentication throttle unavailable")
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
        now = timezone.now()
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
        account_retry = _retry_after(bucket=buckets.get(LoginThrottleBucket.Kind.ACCOUNT), now=now)
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

    def record_failure(self, *, username: str, client_ip: str) -> FailureRecord:
        account_key, ip_key = self._keys(username=username, client_ip=client_ip)
        now = timezone.now()
        values = (
            (LoginThrottleBucket.Kind.ACCOUNT, account_key, ACCOUNT_FAILURE_LIMIT),
            (LoginThrottleBucket.Kind.IP, ip_key, IP_FAILURE_LIMIT),
        )
        newly_blocked: str | None = None
        first_ip_failure = False
        with transaction.atomic():
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

    def record_success(self, *, username: str) -> None:
        account_key = _digest(
            secret=_secret(),
            kind=LoginThrottleBucket.Kind.ACCOUNT,
            value=_account_value(username),
        )
        with transaction.atomic():
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
