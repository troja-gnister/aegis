"""Crash recovery for the derivatives directory: orphan files and orphan rows (spec M2).

Publishing places a file before it commits the row that names it (``publish.py``), and
eviction commits the row deletion before it unlinks the file (``eviction.py``). A crash, a
rollback or an operator's mistake can therefore leave:

- **orphan files**: ``<key>.<ext>`` with no ``ready`` or ``stale`` row of that key and
  extension, and ``.<key>.<random>.tmp`` staging copies. They are removed once older than
  :data:`ORPHAN_GRACE_SECONDS`, and a final name only under its key's advisory lock and after
  re-checking that no row claims it, so a publication in progress is never touched;
- **orphan rows**: a ``ready`` or ``stale`` row whose file is missing. A stale row, or an
  evictable (preview or transcode) row, is deleted; a later request rebuilds it. A current
  thumbnail or poster row is marked ``stale`` and its asset stage goes back to ``pending``,
  so the pipeline rebuilds it.

The sweep starts with the worker and repeats every :data:`RECOVERY_INTERVAL_SECONDS`. It is
incremental: one ``<aa>/<bb>`` directory, or one keyset batch of rows, per step. It never
waits for a lock (``SKIP LOCKED`` and try-locks), so it cannot block publication or
discovery.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from django.db import transaction
from django.db.models import F

from .enums import DerivativeState, MediaKind, StageState
from .models import MediaAsset, MediaDerivative
from .publish import DerivativeStore, StoreUnavailable, try_key_lock

ORPHAN_GRACE_SECONDS: Final = 3_600.0
RECOVERY_INTERVAL_SECONDS: Final = 86_400.0
ROW_BATCH: Final = 500
_PREFIX_RE: Final = re.compile(r"[0-9a-f]{2}")
_FINAL_RE: Final = re.compile(r"([0-9a-f]{64})\.(webp|mp4)")
_TEMPORARY_RE: Final = re.compile(r"\.([0-9a-f]{64})\.[0-9a-f]{16}\.tmp")
_REFERENCING_STATES: Final = (DerivativeState.READY, DerivativeState.STALE)


@dataclass(slots=True)
class RecoveryCounts:
    files_removed: int = 0
    rows_deleted: int = 0
    stages_reset: int = 0


@dataclass(slots=True)
class RecoverySweep:
    store: DerivativeStore
    grace_seconds: float = ORPHAN_GRACE_SECONDS
    interval_seconds: float = RECOVERY_INTERVAL_SECONDS
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    phase: Literal["idle", "files", "rows"] = "idle"
    directories: list[str] = field(default_factory=list)
    cursor: str | None = None
    next_sweep_at: float = 0.0
    totals: RecoveryCounts = field(default_factory=RecoveryCounts)

    @property
    def active(self) -> bool:
        return self.phase != "idle"

    def due(self) -> bool:
        return self.phase == "idle" and self.clock() >= self.next_sweep_at

    def start(self) -> None:
        """List the ``<aa>/<bb>`` directories to visit; then sweep rows by key."""
        self.directories = self._directories()
        self.cursor = None
        self.phase = "files"

    def _finish(self) -> None:
        self.phase = "idle"
        self.directories = []
        self.cursor = None
        self.next_sweep_at = self.clock() + self.interval_seconds

    def _directories(self) -> list[str]:
        found: list[str] = []
        try:
            first_level = sorted(os.listdir(self.store.root))
        except OSError:
            return found
        for first in first_level:
            if _PREFIX_RE.fullmatch(first) is None:
                continue
            path = os.path.join(self.store.root, first)
            try:
                if not os.path.isdir(path) or os.path.islink(path):
                    continue
                second_level = sorted(os.listdir(path))
            except OSError:
                continue
            found.extend(
                first + second for second in second_level if _PREFIX_RE.fullmatch(second)
            )
        return found

    def step(self, *, now: datetime) -> RecoveryCounts:
        counts = RecoveryCounts()
        if self.phase == "files":
            if self.directories:
                self._sweep_directory(self.directories.pop(0), counts)
            if not self.directories:
                self.phase = "rows"
        elif self.phase == "rows":
            if self._sweep_rows(counts, now=now):
                self._finish()
        self.totals.files_removed += counts.files_removed
        self.totals.rows_deleted += counts.rows_deleted
        self.totals.stages_reset += counts.stages_reset
        return counts

    # -- orphan files ---------------------------------------------------------------------

    def _sweep_directory(self, prefix: str, counts: RecoveryCounts) -> None:
        try:
            directory = self.store.directory(prefix + "0" * 60, create=False)
        except (OSError, StoreUnavailable):
            return
        if directory is None:
            return
        try:
            finals: dict[str, tuple[str, str]] = {}
            cutoff = self.wall_clock() - self.grace_seconds
            for name in os.listdir(directory):
                try:
                    observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except OSError:
                    continue
                if observed.st_mtime > cutoff:
                    continue
                final = _FINAL_RE.fullmatch(name)
                if final is not None and final[1].startswith(prefix):
                    finals[final[1]] = (final[2], name)
                elif _TEMPORARY_RE.fullmatch(name) is not None:
                    try:
                        os.unlink(name, dir_fd=directory)
                        counts.files_removed += 1
                    except OSError:
                        pass
            if not finals:
                return
            referenced = {
                (key, extension)
                for key, extension in MediaDerivative.objects.filter(
                    key__in=sorted(finals), state__in=_REFERENCING_STATES,
                ).order_by("key").values_list("key", "extension")
            }
            for key, (extension, name) in sorted(finals.items()):
                if (key, extension) in referenced:
                    continue
                with try_key_lock(key) as held:
                    if not held or MediaDerivative.objects.filter(
                        key=key, extension=extension, state__in=_REFERENCING_STATES,
                    ).exists():
                        continue
                    try:
                        os.unlink(name, dir_fd=directory)
                        counts.files_removed += 1
                    except OSError:
                        pass
        finally:
            os.close(directory)

    # -- orphan rows ----------------------------------------------------------------------

    def _sweep_rows(self, counts: RecoveryCounts, *, now: datetime) -> bool:
        """One keyset batch of rows; returns whether the row phase is complete."""
        query = MediaDerivative.objects.filter(state__in=_REFERENCING_STATES)
        if self.cursor is not None:
            query = query.filter(key__gt=self.cursor)
        rows = list(
            query.order_by("key").values(
                "id", "key", "extension", "state", "kind", "evictable", "asset_id",
                "fingerprint",
            )[:ROW_BATCH]
        )
        for row in rows:
            if not self.store.exists(row["key"], row["extension"]):
                self._repair(row, counts, now=now)
        if len(rows) < ROW_BATCH:
            return True
        self.cursor = rows[-1]["key"]
        return False

    def _repair(
        self, row: Mapping[str, Any], counts: RecoveryCounts, *, now: datetime,
    ) -> None:
        with try_key_lock(row["key"]) as held:
            if not held:
                return
            try:
                with transaction.atomic():
                    current = (
                        MediaDerivative.objects.select_for_update(skip_locked=True)
                        .filter(id=row["id"], state=row["state"])
                        .values("id")
                        .first()
                    )
                    if current is None or self.store.exists(row["key"], row["extension"]):
                        return
                    if row["state"] == DerivativeState.STALE or row["evictable"]:
                        MediaDerivative.objects.filter(id=row["id"]).delete()
                        counts.rows_deleted += 1
                        return
                    MediaDerivative.objects.filter(id=row["id"]).update(
                        state=DerivativeState.STALE,
                    )
                    reset = _reset_stage(row, now=now)
                    if reset is None:
                        # The asset row is busy: undo the stale marking, so the stage never
                        # stays ready without a current row; the next sweep retries.
                        raise _Deferred()
                    if reset:
                        counts.stages_reset += 1
            except _Deferred:
                return


class _Deferred(Exception):
    """A repair that must wait for a busy row; its transaction is rolled back."""


def _reset_stage(row: Mapping[str, Any], *, now: datetime) -> bool | None:
    """A current thumbnail or poster lost its file: rebuild that stage.

    Returns ``None`` when the asset row is locked (the caller rolls back and retries later),
    ``False`` when there is nothing to reopen (another fingerprint, or no ready stage).
    """
    query = MediaAsset.objects.filter(entry_id=row["asset_id"], fingerprint=row["fingerprint"])
    asset = (
        query.select_for_update(skip_locked=True, of=("self",))
        .values("kind", "probe_state", "thumbnail_state", "poster_state")
        .first()
    )
    if asset is None:
        return None if query.exists() else False
    if asset["probe_state"] != StageState.READY:
        return False
    # For a video, one poster job rebuilds both of its frames, so both ready stages reopen.
    values: dict[str, Any] = {}
    if asset["thumbnail_state"] == StageState.READY:
        values["thumbnail_state"] = StageState.PENDING
    if asset["kind"] == MediaKind.VIDEO and asset["poster_state"] == StageState.READY:
        values["poster_state"] = StageState.PENDING
    if not values:
        return False
    return MediaAsset.objects.filter(
        entry_id=row["asset_id"], fingerprint=row["fingerprint"],
    ).update(
        **values,
        next_attempt_at=now,
        environment_failures=0,
        lease_owner=None,
        lease_expires_at=None,
        attempt_token=F("attempt_token") + 1,
        updated_at=now,
    ) == 1
