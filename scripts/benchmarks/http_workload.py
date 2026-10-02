"""Real authenticated HTTP traffic: ten independent cookie jars, fixed request mix.

Each simulated user owns one cookie jar, obtains its CSRF token from the API and logs
in through the gateway with a same-origin POST. Requests use the actual list,
details, filter and status routes. Every non-200 response or transport failure is
recorded as a failure, never as a fast success. Output goes through a bounded queue.
"""
from __future__ import annotations

import csv
import http.cookiejar
import json
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dataset import ROOTS, DirectoryTarget
from .report import Sample

REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 4 << 20
WIDE_LIST_PROBABILITY = 0.40
ALTERNATE_SORTS = (("name", "desc"), ("modified", "asc"), ("modified", "desc"),
                   ("size", "asc"), ("size", "desc"))
ALL_SORTS = (("name", "asc"), *ALTERNATE_SORTS)
LIST_KINDS = frozenset({"directoryPages", "alternateSorts", "basicFilters"})
_COMMON_TIME = {"from": "2020-09-01T00:00:00Z", "before": "2021-03-01T00:00:00Z"}
# Adversarial selectivity: common, rare and zero-hit values per field, explicit
# unknowns, literal wildcard-shaped prefixes and all-field combinations.
FILTER_CASES: dict[str, dict[str, Any]] = {
    "kind-directory": {"kind": ["directory"]},
    "kind-symlink-rare": {"kind": ["symlink"]},
    "kind-file-common": {"kind": ["file"]},
    "type-common": {"type": ["jpg"]},
    "type-rare": {"type": ["xyz"]},
    "type-zero": {"type": ["zzz"]},
    "type-unknown": {"type": ["__unknown__"]},
    "size-common": {"size": {"min": "0", "max": "32768"}},
    "size-rare": {"size": {"min": "1099511627776"}},
    "size-zero": {"size": {"min": "18000000000000000000"}},
    "size-unknown": {"size": {"unknown": True}},
    "modified-common": {"modified": dict(_COMMON_TIME)},
    "modified-zero": {"modified": {"from": "1990-01-01T00:00:00Z",
                                   "before": "1990-01-02T00:00:00Z"}},
    "modified-unknown": {"modified": {"unknown": True}},
    "availability-missing": {"availability": ["missing"]},
    "availability-mixed": {"availability": ["present", "missing"]},
    "availability-inaccessible": {"availability": ["inaccessible"]},
    "prefix-common": {"prefix": "img_"},
    "prefix-rare": {"prefix": "rare-"},
    "prefix-zero": {"prefix": "zzzz-none"},
    "prefix-wildcard": {"prefix": "100%_"},
    "all-common": {"kind": ["file"], "type": ["jpg", "jpeg"],
                   "size": {"min": "0", "max": "65536"}, "modified": dict(_COMMON_TIME),
                   "availability": ["present"], "prefix": "img_"},
    "all-zero": {"kind": ["file"], "type": ["jpg"], "size": {"min": "0", "max": "65536"},
                 "modified": dict(_COMMON_TIME), "availability": ["present"],
                 "prefix": "zzzz"},
}
ZERO_HIT_CASES = frozenset({"type-zero", "size-zero", "modified-zero", "prefix-zero",
                            "all-zero"})


@dataclass(frozen=True, slots=True)
class Operation:
    kind: str
    wide: bool = False
    sort: str = "name"
    order: str = "asc"
    travel: str = "first"
    filter_case: str | None = None

    @property
    def is_list(self) -> bool:
        return self.kind in LIST_KINDS


def plan_operations(profile: Mapping[str, Any], *, seed: int, worker: int,
                    count: int | None = None) -> Iterator[Operation]:
    """Deterministic per-worker operation stream following the profile mix."""
    rng = random.Random(f"aegis-2a1-workload:{seed}:{worker}")
    kinds = sorted(profile["mix"])
    weights = [profile["mix"][kind] for kind in kinds]
    cases = sorted(FILTER_CASES)
    produced = 0
    while count is None or produced < count:
        produced += 1
        kind = rng.choices(kinds, weights)[0]
        if kind not in LIST_KINDS:
            yield Operation(kind)
            continue
        wide = rng.random() < WIDE_LIST_PROBABILITY
        draw = rng.random()
        travel = "first" if draw < 0.35 else "next" if draw < 0.90 else "previous"
        if kind == "directoryPages":
            yield Operation(kind, wide, travel=travel)
        elif kind == "alternateSorts":
            sort, order = ALTERNATE_SORTS[rng.randrange(len(ALTERNATE_SORTS))]
            yield Operation(kind, wide, sort, order, travel)
        else:
            sort, order = ALL_SORTS[rng.randrange(len(ALL_SORTS))]
            yield Operation(kind, wide, sort, order, "first" if draw < 0.6 else "next",
                            cases[rng.randrange(len(cases))])


def filter_parameter(case: str) -> str:
    return json.dumps({"v": 1, **FILTER_CASES[case]}, separators=(",", ":"), sort_keys=True)


class HttpSession:
    """One user: an independent cookie jar, CSRF token and same-origin login."""

    def __init__(self, base_url: str) -> None:
        if not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("benchmark traffic is loopback-only")
        self.base_url = base_url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect(),
        )

    def request(self, method: str, path: str, *, query: Mapping[str, str] | None = None,
                body: object = None, csrf: str | None = None,
                ) -> tuple[int, bytes, float]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers |= {"Content-Type": "application/json", "Origin": self.base_url,
                        "Referer": self.base_url + "/"}
        if csrf is not None:
            headers["X-CSRFToken"] = csrf
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(65_536)
        except (OSError, ValueError):
            status, raw = 0, b""
        return status, raw, (time.perf_counter() - started) * 1000

    def login(self, username: str, password: str, *, attempts: int = 30) -> None:
        for _attempt in range(attempts):
            status, raw, _ = self.request("GET", "/api/v1/auth/csrf")
            token = _json(raw).get("csrfToken") if status == 200 else None
            if isinstance(token, str):
                status, _raw, _ = self.request(
                    "POST", "/api/v1/auth/login",
                    body={"username": username, "password": password}, csrf=token,
                )
                if status == 200:
                    return
                if status not in (429, 503):
                    raise RuntimeError(f"benchmark login failed with HTTP {status}")
            time.sleep(7)  # gateway login rate limit (setup only, never measured)
        raise RuntimeError("benchmark login did not succeed")


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


CONTEXTS_PER_KIND = 8
# A 200 response whose body lacks the route's expected JSON shape is a failure.
INVALID_BODY = -2


@dataclass
class Listing:
    """One browsing context: folder, sort, order and filter, with its live cursors."""

    kind: str
    wide: bool
    sort: str
    order: str
    filter_case: str | None
    target: DirectoryTarget
    next_cursor: str | None = None
    previous_cursor: str | None = None


@dataclass
class Worker:
    """Navigation keeps cursor state per (folder, sort, filter) context.

    A planned next/previous page is issued against a retained context of the same
    request kind that actually offers that cursor (preferring the planned wide/tree
    target class, then the most recent), so it is never silently a first page. Only
    when no retained context can travel that way does it become a first page, and the
    realized travel is what gets recorded.
    """

    number: int
    session: Any
    targets: Sequence[DirectoryTarget]
    wide: DirectoryTarget
    root_ids: Mapping[str, str]
    page_size: int
    rng: random.Random
    listings: dict[str, list[Listing]] = field(default_factory=dict)
    entry_ids: list[str] = field(default_factory=list)
    planned: Counter[tuple[str, str]] = field(default_factory=Counter)
    realized: Counter[tuple[str, str]] = field(default_factory=Counter)

    def _context(self, operation: Operation) -> tuple[Listing, str | None, str]:
        contexts = self.listings.setdefault(operation.kind, [])
        if operation.travel in ("next", "previous"):
            usable = [listing for listing in contexts if (
                listing.next_cursor if operation.travel == "next" else listing.previous_cursor
            )]
            preferred = [listing for listing in usable if listing.wide == operation.wide]
            pool = preferred or usable
            choice = pool[-1] if pool else None
            if choice is not None:
                contexts.remove(choice)
                contexts.append(choice)
                cursor = (choice.next_cursor if operation.travel == "next"
                          else choice.previous_cursor)
                return choice, cursor, operation.travel
        target = self.wide if operation.wide else self.targets[
            self.rng.randrange(len(self.targets))]
        listing = Listing(operation.kind, target.label == "wide", operation.sort,
                          operation.order, operation.filter_case, target)
        contexts.append(listing)
        del contexts[:-CONTEXTS_PER_KIND]
        return listing, None, "first"

    def _list(self, operation: Operation, phase: str) -> Sample:
        listing, cursor, travel = self._context(operation)
        self.planned[(operation.kind, operation.travel)] += 1
        self.realized[(operation.kind, travel)] += 1
        query = {"parent": str(listing.target.id), "limit": str(self.page_size),
                 "sort": listing.sort, "order": listing.order}
        if listing.filter_case is not None:
            query["filters"] = filter_parameter(listing.filter_case)
        if cursor is not None:
            query["cursor"] = cursor
        klass = (f"list:{operation.kind}:{'wide' if listing.wide else 'tree'}:"
                 f"{listing.sort}-{listing.order}:{travel}")
        if listing.filter_case is not None:
            klass += ":filter"
        path = f"/api/v1/roots/{self.root_ids[listing.target.root]}/entries"
        status, raw, latency = self.session.request("GET", path, query=query)
        entries = -1
        if status == 200:
            payload = _json(raw)
            rows = payload.get("entries")
            if not isinstance(rows, list):
                return Sample(phase, "list", klass, INVALID_BODY, latency, len(raw))
            entries = len(rows)
            next_cursor, previous_cursor = payload.get("nextCursor"), payload.get(
                "previousCursor")
            listing.next_cursor = next_cursor if isinstance(next_cursor, str) else None
            listing.previous_cursor = (previous_cursor if isinstance(previous_cursor, str)
                                       else None)
            ids = [row.get("id") for row in rows if isinstance(row, dict)]
            if ids:
                self.entry_ids = [value for value in ids if isinstance(value, str)][:250]
        return Sample(phase, "list", klass, status, latency, len(raw), entries)

    def execute(self, operation: Operation, phase: str) -> Sample:
        if operation.is_list:
            return self._list(operation, phase)
        self.planned[(operation.kind, "-")] += 1
        self.realized[(operation.kind, "-")] += 1
        if operation.kind == "details":
            entry = (self.entry_ids[self.rng.randrange(len(self.entry_ids))]
                     if self.entry_ids else str(self.wide.id))
            status, raw, latency = self.session.request("GET", f"/api/v1/entries/{entry}")
            if status == 200 and not isinstance(_json(raw).get("id"), str):
                status = INVALID_BODY
            return Sample(phase, "details", "details", status, latency, len(raw))
        root = self.targets[self.rng.randrange(len(self.targets))].root
        status, raw, latency = self.session.request(
            "GET", f"/api/v1/roots/{self.root_ids[root]}/index-status",
        )
        if status == 200 and not isinstance(_json(raw).get("state"), str):
            status = INVALID_BODY
        return Sample(phase, "status", "status", status, latency, len(raw))


def mix_report(workers: Sequence[Worker]) -> dict[str, Any]:
    """Planned versus realized operation counts per (kind, travel)."""
    planned: Counter[tuple[str, str]] = Counter()
    realized: Counter[tuple[str, str]] = Counter()
    for worker in workers:
        planned.update(worker.planned)
        realized.update(worker.realized)
    keys = sorted(set(planned) | set(realized))
    return {f"{kind}:{travel}": {"planned": planned[(kind, travel)],
                                 "realized": realized[(kind, travel)]}
            for kind, travel in keys}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """API calls never follow redirects; a 3xx is reported as a failure status."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class SampleSink:
    """Bounded queue drained by one writer into a sanitized CSV and memory."""

    def __init__(self, path: Path, *, capacity: int = 10_000) -> None:
        self.queue: queue.Queue[Sample | None] = queue.Queue(maxsize=capacity)
        self.samples: list[Sample] = []
        self.path = path
        self.thread = threading.Thread(target=self._drain, name="benchmark-sink", daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("phase", "route", "class", "status", "latency_ms", "bytes",
                             "entries"))
            while True:
                sample = self.queue.get()
                if sample is None:
                    return
                self.samples.append(sample)
                writer.writerow((sample.phase, sample.route, sample.klass, sample.status,
                                 f"{sample.latency_ms:.3f}", sample.bytes, sample.entries))

    def put(self, sample: Sample) -> None:
        self.queue.put(sample, timeout=600)

    def close(self) -> list[Sample]:
        self.queue.put(None)
        self.thread.join(timeout=600)
        return self.samples


def accessible_targets(number: int, targets: Sequence[DirectoryTarget]) -> list[DirectoryTarget]:
    roots = {root.key for root in ROOTS if number in (*root.direct_users, *root.group_users)}
    return [target for target in targets
            if target.root in roots and target.label in ("tree", "anchor")]


def run_phase(workers: Sequence[Worker], plans: Sequence[Iterator[Operation]], sink: SampleSink,
              phase: str, *, seconds: float | None = None, operations: int | None = None,
              progress: Callable[[str], None] | None = None) -> None:
    """Run every worker concurrently (one thread each) until the phase bound."""
    deadline = None if seconds is None else time.monotonic() + seconds
    failures: list[BaseException] = []

    def loop(worker: Worker, plan: Iterator[Operation]) -> None:
        try:
            done = 0
            while (deadline is None or time.monotonic() < deadline) and (
                operations is None or done < operations
            ):
                sink.put(worker.execute(next(plan), phase))
                done += 1
        except BaseException as exc:  # surfaced after join; never silently dropped
            failures.append(exc)

    threads = [threading.Thread(target=loop, args=(worker, plan), name=f"bench-{worker.number}")
               for worker, plan in zip(workers, plans, strict=True)]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(timeout=30)
        if progress is not None and any(thread.is_alive() for thread in threads):
            progress(f"{phase}: {time.monotonic() - started:.0f}s elapsed")
    if failures:
        raise RuntimeError("benchmark worker failed") from failures[0]


def run_matrix(session: HttpSession, wide: DirectoryTarget, root_id: str, sink: SampleSink,
               *, page_size: int, repetitions: int = 3) -> dict[str, Any]:
    """Each filter field alone and combined, with every sort, first and next page."""
    results: dict[str, Any] = {}
    for case in (None, *sorted(FILTER_CASES)):
        name = case or "none"
        rows: dict[str, Any] = {}
        for sort, order in ALL_SORTS:
            first_entries = -1
            has_next = False
            for _repetition in range(repetitions):
                query = {"parent": str(wide.id), "limit": str(page_size),
                         "sort": sort, "order": order}
                if case is not None:
                    query["filters"] = filter_parameter(case)
                status, raw, latency = session.request(
                    "GET", f"/api/v1/roots/{root_id}/entries", query=query)
                payload: dict[str, Any] = _json(raw) if status == 200 else {}
                page_rows = payload.get("entries")
                if status == 200 and not isinstance(page_rows, list):
                    status = INVALID_BODY
                entries = (len(page_rows) if status == 200 and isinstance(page_rows, list)
                           else -1)
                sink.put(Sample("matrix", "list", f"matrix:{name}:{sort}-{order}:first",
                                status, latency, len(raw), entries))
                first_entries = entries
                cursor = payload.get("nextCursor")
                has_next = isinstance(cursor, str)
                if has_next:
                    status, raw, latency = session.request(
                        "GET", f"/api/v1/roots/{root_id}/entries",
                        query=query | {"cursor": str(cursor)})
                    next_rows = _json(raw).get("entries") if status == 200 else None
                    if status == 200 and not isinstance(next_rows, list):
                        status = INVALID_BODY
                    entries = len(next_rows) if isinstance(next_rows, list) else -1
                    sink.put(Sample("matrix", "list", f"matrix:{name}:{sort}-{order}:next",
                                    status, latency, len(raw), entries))
            rows[f"{sort}-{order}"] = {"firstPageEntries": first_entries, "hasNext": has_next}
        expected_zero = case in ZERO_HIT_CASES
        observed_zero = all(row["firstPageEntries"] == 0 for row in rows.values())
        results[name] = {"sorts": rows, "expectedZeroHit": expected_zero,
                         "zeroHitConsistent": observed_zero == expected_zero}
    return results


def run_sequential_baseline(session: HttpSession, wide: DirectoryTarget, tree: DirectoryTarget,
                            root_id: str, sink: SampleSink, *, page_size: int,
                            repetitions: int = 100) -> None:
    """One user, one request at a time: service time without queueing, per route."""
    status_path = f"/api/v1/roots/{root_id}/index-status"
    list_path = f"/api/v1/roots/{root_id}/entries"
    entry_ids: list[str] = []
    for _repetition in range(repetitions):
        for name, target in (("wide", wide), ("tree", tree)):
            status, raw, latency = session.request(
                "GET", list_path, query={"parent": str(target.id), "limit": str(page_size)})
            rows = _json(raw).get("entries") if status == 200 else None
            if status == 200 and not isinstance(rows, list):
                status = INVALID_BODY
            if isinstance(rows, list):
                entry_ids = [str(row["id"]) for row in rows
                             if isinstance(row, dict) and isinstance(row.get("id"), str)]
            sink.put(Sample("sequential", "list", f"sequential:list:{name}", status, latency,
                            len(raw), len(rows) if isinstance(rows, list) else -1))
        entry = entry_ids[len(entry_ids) // 2] if entry_ids else str(wide.id)
        status, raw, latency = session.request("GET", f"/api/v1/entries/{entry}")
        if status == 200 and not isinstance(_json(raw).get("id"), str):
            status = INVALID_BODY
        sink.put(Sample("sequential", "details", "sequential:details", status, latency,
                        len(raw)))
        status, raw, latency = session.request("GET", status_path)
        if status == 200 and not isinstance(_json(raw).get("state"), str):
            status = INVALID_BODY
        sink.put(Sample("sequential", "status", "sequential:status", status, latency, len(raw)))
