"""Publishing validated outputs as derivatives (spec M1, M2, M4).

Order, for every publication:

1. **Stage** (outside any transaction): copy each verified output from its scratch descriptor
   into ``<derivatives>/<aa>/<bb>/.<key>.<random>.tmp``, created with ``O_EXCL`` at mode 0640
   relative to the held derivatives root descriptor (no scratch path is ever renamed or
   linked into place); fsync, then check the copy's size, signature and WebP canvas again. A
   source that changed after validation is :class:`OutputRejected` (the processor's fault);
   a failure of the derivatives volume is :class:`PublishFailed`. Directories are created at
   0750, or 2750 when the derivatives directory is setgid (the gateway reads through the
   group; the worker must belong to that group, or ``fchmod`` would clear the setgid bit).
2. **Record** (one transaction): lock the asset row and check its fence (the lease, or for an
   interactive job the fingerprint it was built from); take each key's advisory lock; place
   the file under ``<key>.<ext>`` (``link``, so an existing name is never replaced, except an
   orphan or a stale row's file of the same key, which is replaced atomically by ``rename``);
   insert or revive the ``MediaDerivative`` row with its fingerprint, after marking any other
   current row of that kind ``stale``; then update the asset. If the fence no longer holds,
   nothing is placed and the staged copies are removed.
3. The caller then deletes the job's ``done/`` directory.

Crash safety: a crash after step 1 leaves temporary files, and a crash or rollback inside step
2 can leave a placed file without a row. Both are orphans, removed by ``recovery.py`` (after a
grace period and under the key's lock). A row is only ever committed after its file is in
place, so a committed ``ready`` row always had its file. Publishing the same key again (a
retry after a crash, a revived stale row) is idempotent.

Eviction (``eviction.py``) takes the same per-key advisory lock (session-level) around "delete
the row, commit, unlink the file", so it can never unlink a file that a concurrent
publication has just placed for a new row.
"""

from __future__ import annotations

import os
import secrets
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .dispatch import HEADER_BYTES, VerifiedOutput, check_header
from .enums import DerivativeState
from .formats import EVICTABLE_DERIVATIVE_KINDS
from .keys import is_hex_digest, relative_path
from .models import MediaAsset, MediaDerivative

DIRECTORY_MODE: Final = 0o750
FILE_MODE: Final = 0o640
KEY_LOCK_NAMESPACE: Final = 0x4D454449
COPY_CHUNK_BYTES: Final = 1 << 20
COPY_PROGRESS_BYTES: Final = 64 << 20
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS: Final = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
)


class StoreUnavailable(RuntimeError):
    """The derivatives directory is missing, not a plain directory, or not writable."""


class PublishFailed(RuntimeError):
    """A file could not be staged or placed; the message carries no path."""

    def __init__(self) -> None:
        super().__init__("media_publish_failed")


class OutputRejected(ValueError):
    """The scratch output changed, or could not be read, after it was validated.

    Unlike :class:`PublishFailed` (the derivatives volume), this is the processor's doing:
    the worker records it as ``output_invalid``, which counts toward quarantine.
    """

    def __init__(self) -> None:
        super().__init__("media_output_rejected")


class FenceLost(RuntimeError):
    """The asset changed or the lease was lost; nothing was recorded."""

    def __init__(self) -> None:
        super().__init__("media_fence_lost")


@dataclass(slots=True)
class StagedFile:
    key: str
    extension: str
    temporary: str
    directory: int
    bytes: int
    width: int
    height: int
    placed: bool = False


@dataclass(frozen=True, slots=True)
class Publication:
    """One derivative to record: its kind, key and parameters, and its staged copy."""

    kind: str
    key: str
    params_digest: str
    staged: StagedFile


def key_lock_id(key: str) -> int:
    """A signed 32-bit advisory lock id for a derivative key (collisions only serialize)."""
    if not is_hex_digest(key):
        raise ValueError("invalid derivative key")
    return int.from_bytes(bytes.fromhex(key[:8]), "big", signed=True)


def lock_key_for_transaction(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s::integer, %s::integer)",
            [KEY_LOCK_NAMESPACE, key_lock_id(key)],
        )


@contextmanager
def try_key_lock(key: str) -> Iterator[bool]:
    """A session-level, non-blocking key lock (held across a commit); yields whether held."""
    lock_id = key_lock_id(key)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s::integer, %s::integer)",
            [KEY_LOCK_NAMESPACE, lock_id],
        )
        row = cursor.fetchone()
    held = row is not None and row[0] is True
    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s::integer, %s::integer)",
                    [KEY_LOCK_NAMESPACE, lock_id],
                )


def _normalize_directory(descriptor: int) -> None:
    observed = os.fstat(descriptor)
    wanted = DIRECTORY_MODE | (observed.st_mode & stat.S_ISGID)
    if stat.S_IMODE(observed.st_mode) != wanted:
        os.fchmod(descriptor, wanted)


def _subdirectory(parent: int, name: str, *, create: bool) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            return None
    with suppress(FileExistsError):
        os.mkdir(name, DIRECTORY_MODE, dir_fd=parent)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _normalize_directory(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


class DerivativeStore:
    """``<derivatives>/<key[0:2]>/<key[2:4]>/<key>.<ext>``, opened without following links.

    The derivatives root is opened once (``O_DIRECTORY|O_NOFOLLOW``) and every directory and
    file beneath it is opened relative to that descriptor. :meth:`check` re-opens it if the
    path now names another directory (a remount).
    """

    def __init__(self, root: str) -> None:
        if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
            raise ValueError("the derivatives directory must be an absolute path")
        self.root = os.path.normpath(root)
        self._root_descriptor = -1

    def close(self) -> None:
        if self._root_descriptor >= 0:
            with suppress(OSError):
                os.close(self._root_descriptor)
            self._root_descriptor = -1

    def _root(self) -> int:
        if self._root_descriptor < 0:
            try:
                self._root_descriptor = os.open(self.root, _DIRECTORY_FLAGS)
            except OSError as error:
                raise StoreUnavailable("the derivatives directory is unavailable") from error
        return self._root_descriptor

    def check(self) -> None:
        try:
            observed = os.stat(self.root, follow_symlinks=False)
        except OSError as error:
            self.close()
            raise StoreUnavailable("the derivatives directory is unavailable") from error
        if not stat.S_ISDIR(observed.st_mode) or not os.access(
            self.root, os.W_OK | os.X_OK, effective_ids=True,
        ):
            self.close()
            raise StoreUnavailable("the derivatives directory is not a writable directory")
        try:
            held = os.fstat(self._root())
        except OSError as error:
            self.close()
            raise StoreUnavailable("the derivatives directory is unavailable") from error
        if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
            self.close()
            self._root()

    def directory(self, key: str, *, create: bool) -> int | None:
        """A descriptor of the key's ``<aa>/<bb>`` directory (the caller closes it)."""
        if not is_hex_digest(key):
            raise ValueError("invalid derivative key")
        first = _subdirectory(self._root(), key[0:2], create=create)
        if first is None:
            return None
        try:
            return _subdirectory(first, key[2:4], create=create)
        finally:
            os.close(first)

    def stage(
        self,
        output: VerifiedOutput,
        key: str,
        extension: str,
        *,
        progress: Callable[[], bool] | None = None,
    ) -> StagedFile:
        """Copy a verified output into a fresh temporary file next to its final name.

        Raises :class:`OutputRejected` when the source no longer matches what was validated,
        and :class:`PublishFailed` for the derivatives volume (or when ``progress``, called
        between chunks, returns ``False``).
        """
        relative_path(key, extension)
        if extension != output.format:
            raise PublishFailed()
        try:
            directory = self.directory(key, create=True)
        except (OSError, StoreUnavailable):
            raise PublishFailed() from None
        if directory is None:
            raise PublishFailed()
        temporary = f".{key}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(temporary, _CREATE_FLAGS, FILE_MODE, dir_fd=directory)
        except OSError:
            os.close(directory)
            raise PublishFailed() from None

        def discard() -> None:
            os.close(descriptor)
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)

        try:
            os.fchmod(descriptor, FILE_MODE)
            _copy(output, descriptor, progress)
            os.fsync(descriptor)
            copied = os.fstat(descriptor)
            head = os.pread(descriptor, HEADER_BYTES, 0)
        except OutputRejected:
            discard()
            raise
        except (OSError, PublishFailed):
            discard()
            raise PublishFailed() from None
        if copied.st_size != output.bytes or not check_header(
            output.format, head, size=output.bytes, width=output.width, height=output.height,
        ):
            discard()
            raise OutputRejected()
        os.close(descriptor)
        return StagedFile(
            key=key, extension=extension, temporary=temporary, directory=directory,
            bytes=output.bytes, width=output.width, height=output.height,
        )

    def place(self, staged: StagedFile) -> None:
        """Give a staged copy its final name.

        ``link`` never replaces a name. If the name exists, ``rename`` replaces it atomically:
        the caller (``_record``, holding the key's lock) has already found that no ready row
        of this key still names a file there, so what is replaced is an orphan or a stale
        row's file of the same key. Both names are inside the worker's own derivatives tree.
        """
        final = f"{staged.key}.{staged.extension}"
        try:
            try:
                os.link(
                    staged.temporary, final, src_dir_fd=staged.directory,
                    dst_dir_fd=staged.directory, follow_symlinks=False,
                )
            except FileExistsError:
                os.rename(
                    staged.temporary, final, src_dir_fd=staged.directory,
                    dst_dir_fd=staged.directory,
                )
            else:
                os.unlink(staged.temporary, dir_fd=staged.directory)
            os.fsync(staged.directory)
        except OSError:
            raise PublishFailed() from None
        staged.placed = True

    def release(self, staged: StagedFile) -> None:
        """Remove an unplaced temporary copy and close its directory; never raises."""
        if not staged.placed:
            with suppress(OSError):
                os.unlink(staged.temporary, dir_fd=staged.directory)
        if staged.directory >= 0:
            with suppress(OSError):
                os.close(staged.directory)
            staged.directory = -1

    def exists(self, key: str, extension: str) -> bool:
        relative_path(key, extension)
        try:
            directory = self.directory(key, create=False)
        except (OSError, StoreUnavailable):
            return False
        if directory is None:
            return False
        try:
            observed = os.stat(f"{key}.{extension}", dir_fd=directory, follow_symlinks=False)
        except OSError:
            return False
        finally:
            os.close(directory)
        return stat.S_ISREG(observed.st_mode)

    def remove(self, key: str, extension: str) -> bool:
        """Unlink ``<key>.<ext>``; ``True`` when it is gone (or was never there)."""
        relative_path(key, extension)
        try:
            directory = self.directory(key, create=False)
        except (OSError, StoreUnavailable):
            return False
        if directory is None:
            return True
        try:
            os.unlink(f"{key}.{extension}", dir_fd=directory)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        finally:
            os.close(directory)
        return True


def _copy(
    output: VerifiedOutput, target: int, progress: Callable[[], bool] | None = None,
) -> None:
    """Copy exactly ``output.bytes`` from the scratch descriptor; a longer file is refused.

    Reading the source can only fail through the processor (:class:`OutputRejected`);
    writing can only fail through the derivatives volume (``OSError``). ``progress`` runs
    every :data:`COPY_PROGRESS_BYTES` so a long copy can renew leases or be abandoned.
    """
    copied = 0
    reported = 0
    while copied < output.bytes:
        try:
            chunk = os.pread(
                output.descriptor, min(COPY_CHUNK_BYTES, output.bytes - copied), copied,
            )
        except OSError:
            raise OutputRejected() from None
        if not chunk:
            raise OutputRejected()
        view = memoryview(chunk)
        while view:
            view = view[os.write(target, view):]
        copied += len(chunk)
        if progress is not None and copied - reported >= COPY_PROGRESS_BYTES:
            reported = copied
            if not progress():
                raise PublishFailed()
    try:
        longer = os.pread(output.descriptor, 1, copied)
    except OSError:
        raise OutputRejected() from None
    if longer:
        raise OutputRejected()


def stage_outputs(
    store: DerivativeStore,
    outputs: Sequence[tuple[VerifiedOutput, str]],
    *,
    progress: Callable[[], bool] | None = None,
) -> list[StagedFile]:
    """Stage every ``(output, key)``; on failure, nothing staged is left behind."""
    staged: list[StagedFile] = []
    try:
        for output, key in outputs:
            staged.append(store.stage(output, key, output.format, progress=progress))
    except BaseException:
        for item in staged:
            store.release(item)
        raise
    return staged


def record_publications(
    store: DerivativeStore,
    *,
    entry_id: uuid.UUID,
    fingerprint: str,
    fence: Q,
    publications: Sequence[Publication],
    asset_update: Mapping[str, Any] | None,
    now: datetime,
) -> None:
    """Place the staged files and record rows and the asset in one transaction.

    Raises :class:`FenceLost` (nothing placed or recorded) or :class:`PublishFailed`. The
    staged copies are always released.
    """
    try:
        with transaction.atomic():
            locked = (
                MediaAsset.objects.select_for_update(of=("self",))
                .filter(fence)
                .filter(entry_id=entry_id, fingerprint=fingerprint)
                .values("entry_id")
                .first()
            )
            if locked is None:
                raise FenceLost()
            for publication in sorted(publications, key=lambda item: item.key):
                lock_key_for_transaction(publication.key)
            for publication in publications:
                _record(store, entry_id, fingerprint, publication, now=now)
            if asset_update is not None:
                updated = MediaAsset.objects.filter(fence).filter(
                    entry_id=entry_id, fingerprint=fingerprint,
                ).update(**asset_update)
                if updated != 1:
                    raise FenceLost()
    finally:
        for publication in publications:
            store.release(publication.staged)


def _record(
    store: DerivativeStore,
    entry_id: uuid.UUID,
    fingerprint: str,
    publication: Publication,
    *,
    now: datetime,
) -> None:
    staged = publication.staged
    existing = (
        MediaDerivative.objects.select_for_update()
        .filter(key=publication.key)
        .values("id", "state", "asset_id", "fingerprint", "extension")
        .first()
    )
    if existing is not None and (
        existing["asset_id"] != entry_id or existing["fingerprint"] != fingerprint
    ):
        # The key covers the fingerprint, which covers the entry; this cannot happen.
        raise PublishFailed()
    MediaDerivative.objects.filter(asset_id=entry_id, kind=publication.kind).exclude(
        state=DerivativeState.STALE,
    ).exclude(key=publication.key).update(state=DerivativeState.STALE)
    if (
        existing is not None
        and existing["state"] == DerivativeState.READY
        and store.exists(publication.key, staged.extension)
    ):
        # Already published (a retry after a crash): keep the file and the row.
        return
    store.place(staged)
    values: dict[str, Any] = {
        "asset_id": entry_id,
        "kind": publication.kind,
        "fingerprint": fingerprint,
        "params_digest": publication.params_digest,
        "extension": staged.extension,
        "bytes": staged.bytes,
        "width": staged.width,
        "height": staged.height,
        "state": DerivativeState.READY,
        "evictable": publication.kind in EVICTABLE_DERIVATIVE_KINDS,
        "last_access_at": now,
    }
    if existing is None:
        MediaDerivative.objects.create(key=publication.key, **values)
    else:
        # A republished key gets a fresh created_at: delivery's strong ETag is the key plus
        # the size plus created_at, so a browser never keeps the bytes of an earlier file.
        MediaDerivative.objects.filter(id=existing["id"]).update(**values, created_at=now)


def record_failed_request(
    *,
    entry_id: uuid.UUID,
    fingerprint: str,
    kind: str,
    key: str,
    params_digest: str,
    extension: str,
    fence: Q,
    now: datetime | None = None,
) -> bool:
    """Record a failed interactive derivative, unless a current one is ready.

    The details endpoint reports ``failed`` from this row (the web side's request backoff
    reads it) until a later request succeeds, which revives the same key.
    """
    recorded_at = timezone.now() if now is None else now
    with transaction.atomic():
        locked = (
            MediaAsset.objects.select_for_update(of=("self",))
            .filter(fence)
            .filter(entry_id=entry_id, fingerprint=fingerprint)
            .values("entry_id")
            .first()
        )
        if locked is None:
            return False
        lock_key_for_transaction(key)
        current = (
            MediaDerivative.objects.select_for_update()
            .filter(asset_id=entry_id, kind=kind)
            .exclude(state=DerivativeState.STALE)
            .values("id", "key", "state", "fingerprint")
            .first()
        )
        if (
            current is not None
            and current["state"] == DerivativeState.READY
            and current["fingerprint"] == fingerprint
        ):
            return False
        if current is not None and current["key"] != key:
            MediaDerivative.objects.filter(id=current["id"]).update(state=DerivativeState.STALE)
        values: dict[str, Any] = {
            "asset_id": entry_id,
            "kind": kind,
            "fingerprint": fingerprint,
            "params_digest": params_digest,
            "extension": extension,
            "bytes": None,
            "width": None,
            "height": None,
            "state": DerivativeState.FAILED,
            "evictable": kind in EVICTABLE_DERIVATIVE_KINDS,
        }
        existing = MediaDerivative.objects.select_for_update().filter(key=key).values("id").first()
        if existing is None:
            MediaDerivative.objects.create(key=key, **values)
        else:
            MediaDerivative.objects.filter(id=existing["id"]).update(
                **values, created_at=recorded_at,
            )
    return True
