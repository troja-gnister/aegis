from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from aegis_apps.identity.models import LoginThrottleBucket
from aegis_apps.identity.throttling import LoginThrottle
from django.db import close_old_connections
from django.test import override_settings

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
TEST_HMAC_KEY = "task7-test-hmac-key-" + "a" * 48


@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=TEST_HMAC_KEY)
def test_account_bucket_blocks_after_five_failures_without_storing_username() -> None:
    throttle = LoginThrottle()
    for _ in range(5):
        throttle.record_failure(username="Alice", client_ip="192.0.2.10")

    decision = throttle.check(username="alice", client_ip="192.0.2.11")

    assert decision.allowed is False
    assert decision.bucket_type == "account"
    assert decision.retry_after_seconds > 0
    assert not decision.account_key.endswith("alice")
    bucket = LoginThrottleBucket.objects.get(kind=LoginThrottleBucket.Kind.ACCOUNT)
    assert bytes(bucket.key_digest) != b"alice"
    assert "alice" not in str(bucket.__dict__).casefold()


@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=TEST_HMAC_KEY)
def test_ip_bucket_blocks_after_twenty_failures_and_ignores_account_success() -> None:
    throttle = LoginThrottle()
    for index in range(20):
        throttle.record_failure(
            username=f"different-{index}",
            client_ip="192.0.2.20",
        )

    throttle.record_success(username="different-19")
    decision = throttle.check(username="new-account", client_ip="192.0.2.20")

    assert decision.allowed is False
    assert decision.bucket_type == "ip"
    assert decision.retry_after_seconds > 0
    assert LoginThrottleBucket.objects.filter(kind=LoginThrottleBucket.Kind.IP).count() == 1


@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=TEST_HMAC_KEY)
def test_success_clears_only_normalized_account_bucket() -> None:
    throttle = LoginThrottle()
    throttle.record_failure(username="ÅLICE", client_ip="2001:db8::1")

    throttle.record_success(username="ålice")

    assert not LoginThrottleBucket.objects.filter(kind=LoginThrottleBucket.Kind.ACCOUNT).exists()
    assert LoginThrottleBucket.objects.filter(kind=LoginThrottleBucket.Kind.IP).exists()


@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=TEST_HMAC_KEY)
def test_concurrent_first_failures_are_serialized_without_lost_updates() -> None:
    def fail_once(_index: int) -> None:
        close_old_connections()
        try:
            LoginThrottle().record_failure(
                username="concurrent-account",
                client_ip="198.51.100.25",
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fail_once, range(20)))

    account = LoginThrottleBucket.objects.get(kind=LoginThrottleBucket.Kind.ACCOUNT)
    ip = LoginThrottleBucket.objects.get(kind=LoginThrottleBucket.Kind.IP)
    assert account.failures == 5
    assert account.blocked_until is not None
    assert ip.failures == 20
    assert ip.blocked_until is not None


@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=None)
def test_missing_hmac_key_fails_closed_only_when_throttle_is_used() -> None:
    throttle = LoginThrottle()

    with pytest.raises(RuntimeError, match="authentication throttle unavailable"):
        throttle.check(username="alice", client_ip="192.0.2.1")

    assert LoginThrottleBucket.objects.count() == 0
