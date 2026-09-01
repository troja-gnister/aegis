from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID

from aegis.config import ConfigurationError
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from aegis_apps.audit.services import record_event

from .models import User

_BOOTSTRAP_ERROR = "administrator bootstrap configuration is invalid"
_LOCK_DOMAIN = b"aegis.identity.bootstrap-admin.v1\x00"
_REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    user_id: UUID
    created: bool


def _invalid_configuration() -> ConfigurationError:
    return ConfigurationError(_BOOTSTRAP_ERROR)


def _normalize_inputs(
    *, username: str, email: str, password: str, request_id: str
) -> tuple[str, str]:
    if not all(isinstance(value, str) for value in (username, email, password, request_id)):
        raise _invalid_configuration()

    normalized_username = User.normalize_username(username)
    normalized_email = User.objects.normalize_email(email)
    try:
        password_size = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid_configuration() from None

    if (
        not normalized_username
        or len(normalized_username) > 150
        or not normalized_email
        or len(normalized_email) > 254
        or password_size == 0
        or password_size > 4096
        or _REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise _invalid_configuration()

    candidate = User(
        username=normalized_username,
        email=normalized_email,
        is_active=True,
        is_staff=True,
        is_superuser=True,
    )
    try:
        candidate.full_clean(exclude={"password"}, validate_unique=False)
    except ValidationError:
        raise _invalid_configuration() from None
    return normalized_username, normalized_email


def _advisory_lock_key(normalized_username: str) -> int:
    digest = hashlib.sha256(_LOCK_DOMAIN + normalized_username.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def bootstrap_admin(
    *, username: str, email: str, password: str, request_id: str
) -> BootstrapResult:
    """Create an administrator once without ever mutating an existing account."""
    normalized_username, normalized_email = _normalize_inputs(
        username=username,
        email=email,
        password=password,
        request_id=request_id,
    )
    lock_key = _advisory_lock_key(normalized_username)

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])

            try:
                existing = User.objects.get(username=normalized_username)
            except User.DoesNotExist:
                existing = None

            if existing is not None:
                existing_email = User.objects.normalize_email(existing.email)
                if not (
                    existing.is_active
                    and existing.is_staff
                    and existing.is_superuser
                    and existing_email == normalized_email
                ):
                    raise _invalid_configuration()
                user = existing
                result = BootstrapResult(user_id=existing.pk, created=False)
            else:
                candidate = User(
                    username=normalized_username,
                    email=normalized_email,
                    is_active=True,
                    is_staff=True,
                    is_superuser=True,
                )
                try:
                    validate_password(password, user=candidate)
                except ValidationError:
                    raise _invalid_configuration() from None
                candidate.set_password(password)
                candidate.save(force_insert=True)
                user = candidate
                result = BootstrapResult(user_id=candidate.pk, created=True)

            record_event(
                event_type=(
                    "identity.bootstrap.created"
                    if result.created
                    else "identity.bootstrap.existing"
                ),
                outcome="success",
                actor=user,
                request_id=request_id,
                object_id=user.pk,
                metadata={"subject_id": str(user.pk)},
            )
            return result
    except IntegrityError:
        raise _invalid_configuration() from None
