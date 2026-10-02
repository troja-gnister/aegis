"""Sanitized benchmark summaries, query plans and private report files.

Reports never contain credentials, cookies, raw entry names, filter literals or private
paths. Query plans keep only an allowlist of structural/timing fields, and literals in
conditions are replaced. Latency percentiles are computed over successful requests
only; every failed, denied or transport-broken request counts in the error rate.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GATE_P95_MS = 300.0
GATE_PHASES = ("warm",)
REDACTED = "[REDACTED]"
_REPORT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\.(?:json|csv|md)\Z", re.ASCII)
_SENSITIVE_KEY = re.compile(r"password|secret|token|cookie|csrf|session|credential",
                            re.IGNORECASE)
_COOKIE = re.compile(r"\b(?:sessionid|csrftoken|aegis_[a-z_]*)=[^;\s\"]*", re.IGNORECASE)
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_$\"])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z", re.ASCII)
_PLAN_NUMBERS = frozenset({
    "Startup Cost", "Total Cost", "Plan Rows", "Plan Width", "Actual Startup Time",
    "Actual Total Time", "Actual Rows", "Actual Loops", "Rows Removed by Filter",
    "Rows Removed by Index Recheck", "Rows Removed by Join Filter", "Heap Fetches",
    "Sort Space Used", "Hash Buckets", "Hash Batches", "Original Hash Buckets",
    "Original Hash Batches", "Peak Memory Usage", "Shared Hit Blocks", "Shared Read Blocks",
    "Shared Dirtied Blocks", "Shared Written Blocks", "Local Hit Blocks", "Local Read Blocks",
    "Local Dirtied Blocks", "Local Written Blocks", "Temp Read Blocks", "Temp Written Blocks",
    "Shared I/O Read Time", "Shared I/O Write Time", "I/O Read Time", "I/O Write Time",
    "Planning Time", "Execution Time", "Workers Planned", "Workers Launched",
    "Exact Heap Blocks", "Lossy Heap Blocks", "Cache Hits", "Cache Misses",
    "Cache Evictions", "Cache Overflows", "Presorted Groups",
})
_PLAN_WORDS = frozenset({
    "Node Type", "Strategy", "Partial Mode", "Parent Relationship", "Parallel Aware",
    "Async Capable", "Scan Direction", "Join Type", "Inner Unique", "Sort Method",
    "Sort Space Type", "Command", "Single Copy", "Cache Mode",
})
_PLAN_IDENTIFIERS = frozenset({"Relation Name", "Index Name", "Alias", "CTE Name",
                               "Subplan Name", "Schema"})
_PLAN_CONDITIONS = frozenset({
    "Index Cond", "Recheck Cond", "Filter", "Join Filter", "Hash Cond", "Merge Cond",
    "One-Time Filter", "Sort Key", "Presorted Key", "Group Key", "Cache Key",
})
_PLAN_NESTED = frozenset({"Plan", "Plans", "Planning"})


def _condition(value: str) -> str:
    return _NUMBER.sub("?", _QUOTED.sub("'?'", value))[:2048]


def sanitize_plan(plan: Any) -> Any:
    """Keep structure, index choice, rows, buffers and timings; drop literals and names."""
    if isinstance(plan, list):
        return [sanitize_plan(item) for item in plan[:256]]
    if not isinstance(plan, Mapping):
        return None
    result: dict[str, Any] = {}
    for key, value in plan.items():
        if key in _PLAN_NESTED:
            result[key] = sanitize_plan(value)
        elif key in _PLAN_NUMBERS and type(value) in (int, float):
            result[key] = value
        elif key in _PLAN_WORDS and type(value) in (str, bool):
            result[key] = value if type(value) is bool or _IDENTIFIER.fullmatch(
                value.replace(" ", "_")) else "?"
        elif key in _PLAN_IDENTIFIERS and isinstance(value, str):
            result[key] = value if _IDENTIFIER.fullmatch(value) else "?"
        elif key in _PLAN_CONDITIONS:
            if isinstance(value, str):
                result[key] = _condition(value)
            elif isinstance(value, list):
                result[key] = [_condition(str(item)) for item in value[:32]]
    return result


def redact(value: Any, secret_values: Iterable[str]) -> Any:
    """Recursively remove credential-shaped keys, cookies and known secret values."""
    secrets = tuple(value for value in secret_values if value)
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return _COOKIE.sub(REDACTED, value)
    return value


def _private_directory(directory: Path) -> None:
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("report directory is not private")


def write_text(directory: Path, name: str, text: str, *, secrets: Iterable[str]) -> Path:
    if _REPORT_NAME.fullmatch(name) is None:
        raise ValueError("invalid report name")
    _private_directory(directory)
    values = tuple(value for value in secrets if value)
    if any(value in text for value in values):
        raise ValueError("report contains unredacted secret material")
    target = directory / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target


def write_report(directory: Path, name: str, payload: Any, *,
                 secrets: Iterable[str]) -> Path:
    values = tuple(secrets)
    if _REPORT_NAME.fullmatch(name) is None:
        raise ValueError("invalid report name")
    text = json.dumps(redact(payload, values), indent=2, sort_keys=True, default=str) + "\n"
    return write_text(directory, name, text, secrets=values)


@dataclass(frozen=True, slots=True)
class Sample:
    phase: str
    route: str
    klass: str
    status: int
    latency_ms: float
    bytes: int
    entries: int = -1

    @property
    def ok(self) -> bool:
        return self.status == 200


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of sorted values."""
    if not values:
        return None
    rank = max(1, math.ceil(fraction * len(values)))
    return values[min(rank, len(values)) - 1]


def _stats(samples: list[Sample]) -> dict[str, Any]:
    successful = sorted(sample.latency_ms for sample in samples if sample.ok)
    failures = sum(1 for sample in samples if not sample.ok)
    statuses: dict[str, int] = {}
    for sample in samples:
        statuses[str(sample.status)] = statuses.get(str(sample.status), 0) + 1
    payloads = [sample.bytes for sample in samples if sample.ok]
    rows = [sample.entries for sample in samples if sample.ok and sample.entries >= 0]

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    return {
        "requests": len(samples), "successes": len(successful), "failures": failures,
        "errorRate": failures / len(samples) if samples else 0.0,
        "statuses": dict(sorted(statuses.items())),
        "p50Ms": rounded(percentile(successful, 0.50)),
        "p95Ms": rounded(percentile(successful, 0.95)),
        "p99Ms": rounded(percentile(successful, 0.99)),
        "maxMs": rounded(successful[-1] if successful else None),
        "meanPayloadBytes": round(sum(payloads) / len(payloads), 1) if payloads else None,
        "maxPayloadBytes": max(payloads) if payloads else None,
        "meanRows": round(sum(rows) / len(rows), 2) if rows else None,
    }


def summarize(samples: Iterable[Sample]) -> dict[str, Any]:
    """Per phase: route and class statistics plus the unloaded p95 gate result."""
    phases: dict[str, list[Sample]] = {}
    for sample in samples:
        phases.setdefault(sample.phase, []).append(sample)
    result: dict[str, Any] = {}
    for phase, items in sorted(phases.items()):
        routes: dict[str, list[Sample]] = {}
        classes: dict[str, list[Sample]] = {}
        for sample in items:
            routes.setdefault(sample.route, []).append(sample)
            classes.setdefault(sample.klass, []).append(sample)
        route_stats = {name: _stats(group) for name, group in sorted(routes.items())}
        overall = _stats(items)
        list_stats = route_stats.get("list")
        list_p95 = list_stats["p95Ms"] if list_stats else None
        result[phase] = {
            "overall": overall,
            "routes": route_stats,
            "classes": {name: _stats(group) for name, group in sorted(classes.items())},
            "gate": {
                "label": "unloaded" if phase in GATE_PHASES else phase,
                "thresholdP95Ms": GATE_P95_MS,
                "listP95Ms": list_p95,
                "allRoutesP95Ms": overall["p95Ms"],
                "failures": overall["failures"],
                "routesOverThreshold": sorted(
                    name for name, stats in route_stats.items()
                    if stats["p95Ms"] is not None and stats["p95Ms"] > GATE_P95_MS
                ),
                "passed": bool(
                    list_p95 is not None and list_p95 <= GATE_P95_MS
                    and overall["failures"] == 0
                ),
            },
        }
    return result


def markdown_summary(summary: Mapping[str, Any]) -> str:
    """A short human-readable table built only from the sanitized summary."""
    lines = [
        "# Phase 2A.1 catalog benchmark (sanitized)", "",
        f"Status: {summary.get('evidenceLabel', 'unknown')}", "",
        "| Phase | Route | Requests | Failures | p50 ms | p95 ms | p99 ms | Mean bytes |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase, data in summary.get("phases", {}).items():
        for route, stats in data["routes"].items():
            lines.append(
                f"| {phase} | {route} | {stats['requests']} | {stats['failures']} | "
                f"{stats['p50Ms']} | {stats['p95Ms']} | {stats['p99Ms']} | "
                f"{stats['meanPayloadBytes']} |",
            )
    lines.append("")
    return "\n".join(lines)


def _plan_indexes(node: Any, found: list[str]) -> list[str]:
    if isinstance(node, Mapping):
        name = node.get("Index Name")
        if isinstance(name, str) and name not in found:
            found.append(name)
        for key in ("Plan", "Plans"):
            _plan_indexes(node.get(key), found)
    elif isinstance(node, list):
        for item in node:
            _plan_indexes(item, found)
    return found


def _plan_summary(plans: list[Any]) -> list[dict[str, Any]]:
    result = []
    for plan in plans:
        root = plan[0] if isinstance(plan, list) and plan else {}
        result.append({"executionMs": root.get("Execution Time"),
                       "planningMs": root.get("Planning Time"),
                       "indexes": _plan_indexes(root, [])})
    return result


_SCALARS = (str, int, float, bool, type(None))
_PHASES = ("cold", "warmup", "warm", "sequential", "matrix")
_ROUTES = ("list", "details", "status")
_STATS = ("requests", "failures", "errorRate", "p50Ms", "p95Ms", "p99Ms",
          "meanPayloadBytes", "meanRows")
_CLASS = re.compile(r"(?:list:(?:directoryPages|alternateSorts|basicFilters):(?:wide|tree):"
                    r"(?:name|modified|size)-(?:asc|desc):(?:first|next|previous)(?::filter)?"
                    r"|details|status)\Z", re.ASCII)
_MIX = re.compile(r"(?:directoryPages|alternateSorts|basicFilters):(?:first|next|previous)"
                  r"|(?:details|indexStatus):-\Z", re.ASCII)
_LABEL = re.compile(r"[a-z0-9-]{1,64}\Z", re.ASCII)
_INDEX = re.compile(r"catalog_[a-z0-9_]{1,60}\Z", re.ASCII)
_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}\Z", re.ASCII)
# Free text from fixed generator strings: no "/" so a path can never pass.
_WORD = re.compile(r"[A-Za-z0-9 ._:;()',+%-]{0,600}\Z", re.ASCII)
_ROTATIONAL = ("no (SSD/NVMe)", "yes", "unknown")
_KINDS = ("directory", "file", "symlink", "special")
_STATES = ("present", "missing", "inaccessible", "unsupported")
_ROOT_KEYS = ("main", "second", "third")
_SERVICES = ("postgres", "migrate", "web", "operations", "indexer", "media", "gateway")


def _scalar(value: Any, pattern: re.Pattern[str] | None = None) -> Any:
    """Only bounded scalars pass; strings must match the field's allowlisted shape."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and (pattern or _WORD).fullmatch(value):
        return value
    return None


def _pick(source: Any, keys: tuple[str, ...], pattern: re.Pattern[str] | None = None,
          ) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    return {key: _scalar(source.get(key), pattern) for key in keys if key in source}


def _counts(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in _pick(source, keys).items() if type(value) is int}


def verification_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The committed summary: built only from explicitly allowlisted keys and shapes.

    No plan bodies, container IDs, addresses, paths, entry names, users or secrets can
    reach it; a new field in the full report is ignored until it is added here.
    """
    raw_phases = summary.get("phases")
    phases: Mapping[str, Any] = raw_phases if isinstance(raw_phases, Mapping) else {}
    environment = summary.get("environment") or {}
    evidence = summary.get("queryEvidence") or {}
    counts = summary.get("datasetCounts") or {}
    verification = summary.get("verification") or {}
    cpu = summary.get("cpuPlacement") or {}
    bottleneck = summary.get("bottleneck") or {}
    sizes = evidence.get("sizes") or {}
    warm = phases.get("warm") or {}
    utilization = (summary.get("utilization") or {}).get("phases") or {}
    return {
        "version": 2,
        "package": _scalar(summary.get("package")), "mode": _scalar(summary.get("mode")),
        "evidenceLabel": _scalar(summary.get("evidenceLabel")),
        "referenceCertification": _scalar(summary.get("referenceCertification")),
        "startedAt": _scalar(summary.get("startedAt")),
        "shape": _counts(summary.get("shape"), ("entries", "wideFolder", "seed")),
        "durations": _counts(summary.get("durations"), (
            "warmupSeconds", "measurementSeconds", "coldOperationsPerUser")),
        "cpuPlacement": {
            "requested": _scalar(cpu.get("requested")),
            "applied": _pick(cpu.get("applied"), ("mode", "value")),
            "note": _scalar(cpu.get("note")),
        },
        "clientModel": _scalar(summary.get("clientModel")),
        "fixtureControl": _scalar(summary.get("fixtureControl")),
        "postSeedMaintenance": _scalar(summary.get("postSeedMaintenance")),
        "optimisticFixtureSteps": _scalar(summary.get("optimisticFixtureSteps")),
        "workerLoad": _scalar(summary.get("workerLoad")),
        "signingKeySource": _scalar(evidence.get("signingKeySource")),
        "datasetCounts": {
            "entries": _scalar(counts.get("entries")),
            "kinds": _counts(counts.get("kinds"), _KINDS),
            "states": _counts(counts.get("states"), _STATES),
            "roots": _counts(counts.get("roots"), _ROOT_KEYS),
        },
        "seedSeconds": _scalar((summary.get("timings") or {}).get("seedSeconds")),
        "verification": {
            "rootGrantsMatch": verification.get("rootGrantsMatch") is True,
            "indexStatusReady": verification.get("indexStatusReady") is True,
            "wideFolder": _counts(verification.get("wideFolder"), (
                "walkedEntries", "effectivePresent", "orderVerifiedEntries")),
            "staleFixturesUnavailable": [
                [state for state in group if state in _STATES]
                for group in verification.get("staleFixturesUnavailable") or []
                if isinstance(group, list)
            ],
            "ungrantedAccessDenied": _counts(verification.get("ungrantedAccessDenied"), (
                "crossRoot", "adminRootCount", "adminBrowse")),
        },
        "coldRestart": [service for service in summary.get("coldRestart") or []
                        if service in _SERVICES],
        "wideFolderListShare": _scalar(summary.get("wideFolderListShare")),
        "realizedMix": {
            phase: {key: _counts(value, ("planned", "realized"))
                    for key, value in (mix or {}).items() if _MIX.fullmatch(key)}
            for phase, mix in (summary.get("realizedMix") or {}).items() if phase in _PHASES
        },
        "gates": {
            phase: {
                **_pick((phases.get(phase) or {}).get("gate"), (
                    "label", "thresholdP95Ms", "listP95Ms", "allRoutesP95Ms", "failures",
                    "passed", "wideFolderListShare", "wideFolderListShareMinimum")),
                "routesOverThreshold": [
                    route for route in ((phases.get(phase) or {}).get("gate") or {}).get(
                        "routesOverThreshold", []) if route in _ROUTES],
            }
            for phase in _PHASES if phase in phases
        },
        "routes": {
            phase: {route: _pick(stats, _STATS)
                    for route, stats in ((phases.get(phase) or {}).get("routes") or {}).items()
                    if route in _ROUTES}
            for phase in _PHASES if phase in phases
        },
        "warmClasses": {
            name: _pick(stats, ("requests", "p50Ms", "p95Ms", "p99Ms"))
            for name, stats in (warm.get("classes") or {}).items() if _CLASS.fullmatch(name)
        },
        "utilization": {
            phase: {
                service: _pick(stats, (
                    "samples", "cpuMeanPercentOfOneCore", "cpuP95PercentOfOneCore",
                    "cpuMaxPercentOfOneCore", "memoryMeanBytes", "memoryMaxBytes"))
                for service, stats in (services or {}).items() if service in _SERVICES
            }
            for phase, services in utilization.items() if phase in _PHASES
        },
        "bottleneck": {
            "measured": {
                **_pick(bottleneck.get("measured"), (
                    "warmThroughputRequestsPerSecond", "webCpuMeanPercentOfOneCore",
                    "postgresCpuMeanPercentOfOneCore", "webCpuMsPerRequest",
                    "postgresCpuMsPerRequest", "webCpuQuotaCores", "postgresCpuQuotaCores",
                    "concurrentToSequentialListP50")),
                "concurrentP50Ms": _pick((bottleneck.get("measured") or {}).get(
                    "concurrentP50Ms"), _ROUTES),
                "sequentialP50Ms": _pick((bottleneck.get("measured") or {}).get(
                    "sequentialP50Ms"), _ROUTES),
            },
            # Generated by run.bottleneck_evidence from fixed text and numbers only.
            "inferences": [text[:600] for text in bottleneck.get("inferences") or []
                           if isinstance(text, str) and text.startswith("inferred: ")
                           and re.fullmatch(r"[A-Za-z0-9 ._:;,()%+x-]{1,600}", text)],
        },
        "matrixZeroHitConsistent": all(
            isinstance(case, Mapping) and case.get("zeroHitConsistent") is True
            for case in (summary.get("matrix") or {}).values()),
        "queryEvidence": [
            {
                "label": item.get("label") if _LABEL.fullmatch(str(item.get("label"))) else None,
                **_counts(item, ("status", "sqlCount", "payloadBytes", "rows")),
                "catalogQueries": [
                    {"executionMs": _scalar(query.get("executionMs")),
                     "planningMs": _scalar(query.get("planningMs")),
                     "indexes": [name for name in query.get("indexes", [])
                                 if isinstance(name, str) and _INDEX.fullmatch(name)]}
                    for query in _plan_summary(item.get("catalogPlans") or [])
                ],
            }
            for item in evidence.get("requests") or [] if isinstance(item, Mapping)
        ],
        "sizes": {
            **_counts(sizes, (
                "catalogEntryTotalBytes", "catalogEntryHeapBytes", "catalogEntryIndexBytes",
                "catalogEntryEstimatedRows", "catalogAndIndexingTablesTotalBytes",
                "budgetBytes", "sampledRows")),
            "withinBudget": sizes.get("withinBudget") is True,
            "averageRowBytesSampled": _scalar(sizes.get("averageRowBytesSampled")),
            "indexes": [
                {"index": index["index"], **_counts(index, ("bytes", "scans"))}
                for index in sizes.get("indexes") or []
                if isinstance(index, Mapping) and _INDEX.fullmatch(str(index.get("index")))
            ],
        },
        "postgresSettings": _pick(evidence.get("postgresSettings"), _SETTINGS),
        "schemaIdentity": _scalar(evidence.get("schemaIdentity"), _DIGEST),
        "storage": {
            **_pick(summary.get("storage"), (
                "filesystem", "totalBytes", "freeBytes", "requiredFreeBytes",
                "catalogBudgetBytes", "marginBytes", "sufficient")),
            **({"rotational": rotational} if (rotational := (summary.get("storage") or {}).get(
                "rotational")) in _ROTATIONAL else {}),
        },
        "environment": {
            "host": _pick(environment.get("host"), (
                "cpuModel", "logicalCpus", "memoryBytes", "kernel", "machine", "governor",
                "python")),
            "engine": _pick(environment.get("engine"), (
                "engine", "serverVersion", "apiVersion", "kernel", "os", "ncpu", "memTotal",
                "storageDriver", "cgroupVersion", "storage", "storageCalibration")),
            "source": {
                "gitRevision": _scalar((environment.get("source") or {}).get("gitRevision"),
                                       _DIGEST),
                "workingTreeChanges": _scalar((environment.get("source") or {}).get(
                    "workingTreeChanges")),
                **_pick(environment.get("source"), (
                    "uvLockSha256", "npmLockSha256", "imagesLockSha256", "profileSha256"),
                    _DIGEST),
            },
            "containers": {
                service: {
                    **_pick(info, ("imageReference", "memoryLimitBytes", "memorySwapBytes",
                                   "nanoCpus", "cpusetCpus")),
                    "image": _scalar(info.get("image"), _DIGEST),
                }
                for service, info in (environment.get("containers") or {}).items()
                if service in _SERVICES and isinstance(info, Mapping)
            },
        },
    }


def _ms(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _mib(value: Any) -> str:
    return "n/a" if value is None else f"{value / 1024**2:.1f} MiB"


def _gib(value: Any) -> str:
    return "n/a" if value is None else f"{value / 1024**3:.2f} GiB"


def classes_over_threshold(data: Mapping[str, Any], *, minimum_samples: int = 20,
                           ) -> dict[str, Any]:
    """Warm directory-page classes whose own p95 exceeds the threshold (not gated)."""
    gated = (data.get("routes", {}).get("warm", {}).get("list") or {}).get("requests") or 0
    rows = sorted(
        ((name, stats["requests"], stats["p95Ms"])
         for name, stats in (data.get("warmClasses") or {}).items()
         if name.startswith("list:") and (stats.get("requests") or 0) >= minimum_samples
         and (stats.get("p95Ms") or 0) > GATE_P95_MS),
        key=lambda row: -row[2],
    )
    filtered_wide = [p95 for name, _count, p95 in rows
                     if name.startswith("list:basicFilters:wide:")]
    total = sum(count for _name, count, _p95 in rows)
    return {
        "minimumSamples": minimum_samples, "gatedRequests": gated,
        "classes": [{"class": name, "requests": count, "p95Ms": p95,
                     "shareOfGated": round(count / gated, 4) if gated else None}
                    for name, count, p95 in rows],
        "requests": total, "shareOfGated": round(total / gated, 4) if gated else None,
        "filteredWideP95RangeMs": [min(filtered_wide), max(filtered_wide)]
        if filtered_wide else None,
    }


def verification_markdown(data: Mapping[str, Any]) -> str:
    """Human-readable tracked summary derived only from verification_summary()."""
    gate = (data.get("gates") or {}).get("warm") or {}
    warm_list = (data.get("routes") or {}).get("warm", {}).get("list") or {}
    environment = data.get("environment") or {}
    host, engine = environment.get("host") or {}, environment.get("engine") or {}
    source = environment.get("source") or {}
    shape, durations = data.get("shape") or {}, data.get("durations") or {}
    storage, sizes = data.get("storage") or {}, data.get("sizes") or {}
    over = classes_over_threshold(data)
    list_p95 = gate.get("listP95Ms")
    headroom = None if list_p95 is None else GATE_P95_MS - list_p95
    wide_range = over["filteredWideP95RangeMs"]
    lines = [
        "# Phase 2A.1 catalog benchmark (provisional)", "",
        "Generated by `python -m scripts.benchmarks.report verification` from the sanitized "
        "report of `python -m scripts.benchmarks.run catalog`. The machine-readable summary "
        "is [phase-2a-catalog-benchmark.json](phase-2a-catalog-benchmark.json).", "",
        f"- **Evidence label:** {data.get('evidenceLabel')}.",
        f"- **Reference certification:** {data.get('referenceCertification')}.",
        f"- **Workload:** {shape.get('entries')} entries, a {shape.get('wideFolder')}-child "
        f"folder, seed {shape.get('seed')}; {durations.get('warmupSeconds')} s warm-up, "
        f"{durations.get('measurementSeconds')} s measurement, "
        f"{durations.get('coldOperationsPerUser')} cold operations per user.",
        f"- **Started:** {data.get('startedAt')}, source `{source.get('gitRevision')}` with "
        f"{source.get('workingTreeChanges')} uncommitted working-tree changes (test-only "
        "benchmark, seed and documentation files).",
        f"- **Host:** {host.get('cpuModel')}, {host.get('logicalCpus')} logical CPUs, "
        f"{_gib(host.get('memoryBytes'))} RAM, governor {host.get('governor')}, kernel "
        f"{host.get('kernel')}.",
        f"- **Engine:** Docker {engine.get('serverVersion')} ({engine.get('os')}), "
        f"{engine.get('storageDriver')}; {engine.get('storageCalibration')}.",
        f"- **CPU placement:** {(data.get('cpuPlacement') or {}).get('note')}.",
        f"- **Client model:** {data.get('clientModel')}.",
        f"- **Fixture control:** `{data.get('fixtureControl')}`; post-seed step "
        f"`{data.get('postSeedMaintenance')}`. {data.get('optimisticFixtureSteps')}.",
        f"- **Django signing key:** {data.get('signingKeySource') or 'fixed test key'}"
        + ("" if data.get("signingKeySource") else
           " (this run predates the change that makes the benchmark stack use its generated "
           "protected key file)") + ".",
        f"- **Database storage:** {storage.get('filesystem')}, "
        f"{_gib(storage.get('freeBytes'))} free of {_gib(storage.get('totalBytes'))} "
        f"(required {_gib(storage.get('requiredFreeBytes'))}), rotational: "
        f"{storage.get('rotational')}.",
        f"- **Catalog size:** {_gib(sizes.get('catalogAndIndexingTablesTotalBytes'))} of the "
        f"{_gib(sizes.get('budgetBytes'))} budget (within budget: "
        f"{sizes.get('withinBudget')}).", "",
        "## Gate (warm, unloaded, indexed directory pages)", "",
        f"**Directory-page p95 {_ms(list_p95)} ms** against {_ms(gate.get('thresholdP95Ms'))} "
        f"ms, **{_ms(headroom)} ms headroom**; p99 {_ms(warm_list.get('p99Ms'))} ms; "
        f"{warm_list.get('requests')} gated requests, {gate.get('failures')} failures; "
        f"wide-folder list share {gate.get('wideFolderListShare')} (minimum "
        f"{gate.get('wideFolderListShareMinimum')}). **Passed: {gate.get('passed')} "
        "(provisional).**", "",
        "Interpretation: the gate is the aggregate p95 over the specified directory-page "
        "request mix, not a per-class bound. The platform workload defines one fixed mix "
        "with at least a quarter of list traffic on the wide folder and a single warm p95 "
        "(platform spec lines 116 and 645: \"p95 server time for an indexed directory page "
        "is at most 300 ms\"). The 2A.1 design requires measuring and reporting "
        "first/next/previous pages, sorts, filters, details and status (line 175) and "
        "inherits the single 300 ms unloaded value (line 177). Per-class results are "
        "diagnostic and reported below; filtered pages stay in the population. Details and "
        "status are measured but not gated; routes over 300 ms: "
        f"{', '.join(gate.get('routesOverThreshold') or []) or 'none'}.", "",
        "## Classes over 300 ms (not individually gated)", "",
        f"Warm directory-page classes with at least {over['minimumSamples']} samples whose "
        f"own p95 exceeds 300 ms: **{len(over['classes'])} classes, {over['requests']} "
        f"requests, {(over['shareOfGated'] or 0) * 100:.1f}% of the gated population**."
        + (f" Filtered wide-folder pages span **{_ms(wide_range[0])} to {_ms(wide_range[1])} ms "
           "p95** for every sort." if wide_range else "")
        + " Because these classes are only a few percent of the fixed mix, they sit inside "
        "the top-5% tail; the aggregate result is sensitive to the mix and is not a "
        "per-class guarantee.", "",
        "| Class | Requests | Share of gated | p95 ms |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [f"| `{row['class']}` | {row['requests']} | {row['shareOfGated'] * 100:.2f}% | "
              f"{_ms(row['p95Ms'])} |" for row in over["classes"]]
    lines += [""]
    for phase, routes in (data.get("routes") or {}).items():
        lines += [f"## Phase: {phase}", "",
                  "| Route | Requests | Failures | p50 ms | p95 ms | p99 ms | Mean bytes |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        lines += [f"| {route} | {stats.get('requests')} | {stats.get('failures')} | "
                  f"{_ms(stats.get('p50Ms'))} | {_ms(stats.get('p95Ms'))} | "
                  f"{_ms(stats.get('p99Ms'))} | {stats.get('meanPayloadBytes')} |"
                  for route, stats in routes.items()]
        lines += [""]
    lines += ["## Realized request mix (warm)", "",
              "| Kind:travel | Planned | Realized |", "| --- | ---: | ---: |"]
    lines += [f"| {key} | {value.get('planned')} | {value.get('realized')} |"
              for key, value in ((data.get("realizedMix") or {}).get("warm") or {}).items()]
    lines += ["", "## Utilization during measurement (docker stats, 5 s)", "",
              "| Phase | Service | Samples | Mean CPU % of one core | p95 | Max | Peak memory |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for phase, services in (data.get("utilization") or {}).items():
        for service, stats in services.items():
            lines.append(f"| {phase} | {service} | {stats.get('samples')} | "
                         f"{stats.get('cpuMeanPercentOfOneCore')} | "
                         f"{stats.get('cpuP95PercentOfOneCore')} | "
                         f"{stats.get('cpuMaxPercentOfOneCore')} | "
                         f"{_mib(stats.get('memoryMaxBytes'))} |")
    bottleneck = data.get("bottleneck") or {}
    measured = bottleneck.get("measured") or {}
    lines += ["", "## Bottleneck evidence", "",
              f"Measured: {measured.get('warmThroughputRequestsPerSecond')} requests/s; web "
              f"{measured.get('webCpuMsPerRequest')} ms and PostgreSQL "
              f"{measured.get('postgresCpuMsPerRequest')} ms of CPU per request; concurrent "
              f"list p50 is {measured.get('concurrentToSequentialListP50')}x the sequential "
              "p50. Sequential single-user p50 (ms): "
              + ", ".join(f"{route} {_ms(value)}" for route, value in (
                  measured.get("sequentialP50Ms") or {}).items()) + ".", ""]
    lines += [f"- {text}" for text in bottleneck.get("inferences") or []]
    lines += ["", "## Query evidence (in-process, real middleware, aegis_web)", "",
              "| Request | SQL statements | Payload bytes | Rows | Catalog execution ms | "
              "Indexes |", "| --- | ---: | ---: | ---: | --- | --- |"]
    for item in data.get("queryEvidence") or []:
        queries = item.get("catalogQueries") or []
        lines.append(
            f"| {item.get('label')} | {item.get('sqlCount')} | {item.get('payloadBytes')} | "
            f"{item.get('rows', '')} | "
            + ", ".join(_ms(query.get("executionMs")) for query in queries)
            + " | " + ", ".join(sorted({name for query in queries
                                         for name in query.get("indexes", [])})) + " |")
    lines += ["", f"Filter/sort matrix zero-hit consistency: "
              f"{data.get('matrixZeroHitConsistent')}.", ""]
    return "\n".join(lines)


_SETTINGS = (
    "server_version", "shared_buffers", "effective_cache_size", "work_mem",
    "maintenance_work_mem", "random_page_cost", "seq_page_cost", "effective_io_concurrency",
    "max_connections", "jit", "max_parallel_workers_per_gather", "synchronous_commit",
    "default_statistics_target", "huge_pages", "checkpoint_timeout", "max_wal_size",
)


def _statement_kind(sql: str) -> str:
    text = sql.lstrip().upper()
    for prefix in ("WITH", "SELECT", "INSERT", "UPDATE", "DELETE", "SET", "SAVEPOINT",
                   "RELEASE", "ROLLBACK", "COMMIT", "BEGIN"):
        if text.startswith(prefix):
            return prefix
    return "OTHER"


def query_evidence(spec: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover - live stack
    """In-process Django (real middleware, aegis_web login) SQL counts and sanitized plans."""
    import django

    django.setup()
    from aegis_apps.identity.validators import read_private_secret
    from aegis_apps.operations.selectors import current_schema_identity
    from django.conf import settings as django_settings
    from django.db import connection
    from django.test import Client
    from django.test.utils import CaptureQueriesContext

    key_file = os.environ.get("AEGIS_DJANGO_SECRET_KEY_FILE", "")
    signing_key_source = "fixed test key"
    if key_file:
        with open(key_file, encoding="ascii") as handle:
            if handle.read().strip() == django_settings.SECRET_KEY:
                signing_key_source = "generated protected key file"
    password = read_private_secret(Path(str(spec["passwordFile"])))
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    login = client.post("/api/v1/auth/login",
                        {"username": spec["username"], "password": password},
                        content_type="application/json", headers={"X-CSRFToken": token})
    if login.status_code != 200:
        raise RuntimeError("evidence login failed")
    pages: dict[str, dict[str, Any]] = {}
    results = []
    for request in spec["requests"]:
        query = dict(request["query"])
        path = str(request["path"])
        if path.startswith("details-from:"):
            entries = pages[path.removeprefix("details-from:")].get("entries") or []
            path = f"/api/v1/entries/{entries[1]['id']}"
        if request.get("follow"):
            query["cursor"] = pages[request["follow"]]["nextCursor"]
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path, query)
        statements = [str(item["sql"]) for item in captured.captured_queries]
        payload = response.json() if response.status_code == 200 else {}
        pages[str(request["label"])] = payload
        plans = []
        if request.get("explain"):
            for sql in statements:
                if ("catalog_catalogentry" not in sql or "FOR SHARE" in sql.upper()
                        or _statement_kind(sql) not in ("WITH", "SELECT")):
                    continue
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
                    row = cursor.fetchone()
                raw = row[0] if row else None
                plan = json.loads(raw) if isinstance(raw, str) else raw
                plans.append(sanitize_plan(plan))
        results.append({
            "label": request["label"], "status": response.status_code,
            "sqlCount": len(statements),
            "statementKinds": [_statement_kind(sql) for sql in statements],
            "payloadBytes": len(response.content),
            "rows": len(payload.get("entries") or []) if "entries" in payload else None,
            "catalogPlans": plans,
        })
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_total_relation_size('public.catalog_catalogentry'), "
            "pg_relation_size('public.catalog_catalogentry'), "
            "pg_indexes_size('public.catalog_catalogentry'), "
            "(SELECT reltuples::bigint FROM pg_class "
            " WHERE oid = 'public.catalog_catalogentry'::regclass)")
        total, heap, indexes, tuples = cursor.fetchone() or (None, None, None, None)
        cursor.execute(
            "SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relkind = 'r' AND (c.relname LIKE 'catalog\\_%' OR "
            "c.relname LIKE 'indexing\\_%')")
        summed = (cursor.fetchone() or (None,))[0]
        catalog_and_indexing = None if summed is None else int(summed)
        cursor.execute(
            "SELECT indexrelname, pg_relation_size(indexrelid), idx_scan "
            "FROM pg_stat_user_indexes WHERE relname = 'catalog_catalogentry' "
            "ORDER BY indexrelname")
        index_rows = [{"index": name, "bytes": size, "scans": scans}
                      for name, size, scans in cursor.fetchall()]
        cursor.execute(
            "SELECT avg(pg_column_size(c.*))::float8, count(*) FROM "
            "public.catalog_catalogentry c TABLESAMPLE SYSTEM (2) REPEATABLE (7)")
        average_row, sampled = cursor.fetchone() or (None, None)
        cursor.execute("SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s) "
                       "ORDER BY name", [list(_SETTINGS)])
        settings = {name: f"{setting}{unit or ''}" for name, setting, unit in cursor.fetchall()}
    gib = 1024 ** 3
    return {
        "requests": results,
        "sizes": {
            "catalogEntryTotalBytes": total, "catalogEntryHeapBytes": heap,
            "catalogEntryIndexBytes": indexes, "catalogEntryEstimatedRows": tuples,
            "catalogAndIndexingTablesTotalBytes": catalog_and_indexing,
            "budgetBytes": 8 * gib,
            "withinBudget": bool(catalog_and_indexing is not None
                                 and catalog_and_indexing <= 8 * gib),
            "indexes": index_rows,
            "averageRowBytesSampled": average_row, "sampledRows": sampled,
        },
        "postgresSettings": settings,
        "schemaIdentity": current_schema_identity(),
        "signingKeySource": signing_key_source,
    }


def regenerate_verification(report_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    """Rebuild the tracked summary from an existing private report (numbers unchanged)."""
    _private_directory(report_dir)
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    tracked = verification_summary(summary)
    return (write_report(output_dir, "phase-2a-catalog-benchmark.json", tracked, secrets=()),
            write_text(output_dir, "phase-2a-catalog-benchmark.md",
                       verification_markdown(tracked), secrets=()))


def main(arguments: list[str]) -> int:  # pragma: no cover - subprocess entry point
    import sys

    if arguments[:1] == ["verification"] and len(arguments) == 3:
        for path in regenerate_verification(Path(arguments[1]), Path(arguments[2])):
            print(path)
        return 0
    if arguments != ["query-evidence"]:
        raise SystemExit("usage: python -m scripts.benchmarks.report query-evidence | "
                         "verification REPORT_DIR OUTPUT_DIR")
    spec = json.loads(sys.stdin.read(1 << 20))
    print(json.dumps(query_evidence(spec), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))
