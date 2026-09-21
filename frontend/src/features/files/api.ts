import {apiRequest} from "../../api/http";
import {genericApiProblem} from "../../api/problem";
import {
  capturePrivateState,
  isPrivateStateCurrent,
  type PrivateStateSnapshot,
} from "../auth/cache";
import {isSessionAccessOpen} from "../auth/session";
import type {
  BrowseInput,
  DirectoryPage,
  EntryDetails,
  EntryKind,
  EntrySummary,
  FileFilters,
  IndexStatus,
  SourceState,
} from "./types";

const FILE_RESPONSE_BYTES = 1024 * 1024;
const MAX_CURSOR_BYTES = 8192;
const MAX_FILTER_BYTES = 8192;
const MAX_LABEL_LENGTH = 4096;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UNSIGNED_DECIMAL = /^(?:0|[1-9][0-9]*)$/;
const SIGNED_DECIMAL = /^(?:0|-?[1-9][0-9]*)$/;
const TYPE_HINT = /^[a-z0-9]{1,16}$/;
const REQUEST_ID = /^[A-Za-z0-9_-]{8,64}$/;
const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;
const MAX_U64 = 18_446_744_073_709_551_615n;
const MAX_I64 = 9_223_372_036_854_775_807n;
const MIN_I64 = -9_223_372_036_854_775_808n;
const entryKinds = new Set<EntryKind>(["directory", "file", "symlink", "special"]);
const sourceStates = new Set<SourceState>(["present", "missing", "inaccessible", "unsupported"]);
const statusStates = new Set<IndexStatus["state"]>([
  "not_indexed", "queued", "scanning", "ready", "degraded", "unavailable",
]);
const encoder = new TextEncoder();

function invalidResponse(): never {
  throw genericApiProblem(502);
}

function objectWithKeys(value: unknown, expected: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  const fields = value as Record<string, unknown>;
  const keys = Object.keys(fields).sort();
  const sortedExpected = [...expected].sort();
  if (keys.length !== sortedExpected.length || keys.some((key, index) => key !== sortedExpected[index])) {
    invalidResponse();
  }
  return fields;
}

function uuid(value: unknown): string {
  if (typeof value !== "string" || !UUID.test(value)) invalidResponse();
  return value;
}

function label(value: unknown, allowEmpty = false): string {
  if (typeof value !== "string" || (!allowEmpty && value.length < 1) || value.length > MAX_LABEL_LENGTH) {
    invalidResponse();
  }
  return value;
}

function decimal(value: unknown, minimum: bigint, maximum: bigint): string {
  if (typeof value !== "string" || !SIGNED_DECIMAL.test(value)) invalidResponse();
  const parsed = BigInt(value);
  if (parsed < minimum || parsed > maximum) invalidResponse();
  return value;
}

function unsignedDecimal(value: unknown, maximum: bigint): string {
  if (typeof value !== "string" || !UNSIGNED_DECIMAL.test(value)) invalidResponse();
  const parsed = BigInt(value);
  if (parsed > maximum) invalidResponse();
  return value;
}

function nullableDecimal(
  value: unknown,
  minimum: bigint,
  maximum: bigint,
  unsigned = false,
): string | null {
  if (value === null) return null;
  return unsigned ? unsignedDecimal(value, maximum) : decimal(value, minimum, maximum);
}

function timestamp(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string") invalidResponse();
  const match = TIMESTAMP.exec(value);
  if (!match) invalidResponse();
  const [, year, month, day, hour, minute, second, , zone] = match;
  const offset = zone === "Z" ? [0, 0] : zone.slice(1).split(":").map(Number);
  if (
    Number(month) < 1 || Number(month) > 12 || Number(day) < 1 || Number(day) > 31 ||
    Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59 ||
    offset[0]! > 23 || offset[1]! > 59
  ) invalidResponse();
  const calendar = new Date(0);
  calendar.setUTCFullYear(Number(year), Number(month) - 1, Number(day));
  calendar.setUTCHours(0, 0, 0, 0);
  if (calendar.getUTCFullYear() !== Number(year) || calendar.getUTCMonth() !== Number(month) - 1 ||
      calendar.getUTCDate() !== Number(day)) invalidResponse();
  return value;
}

function timestampNanoseconds(value: unknown): bigint {
  const validated = timestamp(value);
  if (validated === null) invalidResponse();
  const match = TIMESTAMP.exec(validated)!;
  const [, year, month, day, hour, minute, second, fraction = "", zone] = match;
  const instant = new Date(0);
  instant.setUTCFullYear(Number(year), Number(month) - 1, Number(day));
  instant.setUTCHours(Number(hour), Number(minute), Number(second), 0);
  let milliseconds = instant.getTime();
  if (zone !== "Z") {
    const [offsetHours, offsetMinutes] = zone.slice(1).split(":").map(Number);
    const offset = (offsetHours! * 60 + offsetMinutes!) * 60_000;
    milliseconds += zone.startsWith("+") ? -offset : offset;
  }
  return BigInt(milliseconds) * 1_000_000n + BigInt(fraction.padEnd(9, "0"));
}

function cursor(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_CURSOR_BYTES ||
      !/^[\x20-\x7e]+$/.test(value) || value.startsWith(".")) invalidResponse();
  return value;
}

const SUMMARY_KEYS = [
  "displayName", "id", "kind", "modifiedNs", "rootId", "size", "sourceState", "typeHint", "version",
] as const;

function validatedSummary(value: unknown, expectedRootId?: string): EntrySummary {
  const fields = objectWithKeys(value, SUMMARY_KEYS);
  const rootId = uuid(fields.rootId);
  if (expectedRootId !== undefined && rootId !== expectedRootId) invalidResponse();
  if (typeof fields.kind !== "string" || !entryKinds.has(fields.kind as EntryKind)) invalidResponse();
  if (fields.typeHint !== null && (typeof fields.typeHint !== "string" || !TYPE_HINT.test(fields.typeHint))) {
    invalidResponse();
  }
  if (typeof fields.sourceState !== "string" || !sourceStates.has(fields.sourceState as SourceState)) {
    invalidResponse();
  }
  return {
    id: uuid(fields.id),
    rootId,
    displayName: label(fields.displayName),
    kind: fields.kind as EntryKind,
    typeHint: fields.typeHint as string | null,
    size: nullableDecimal(fields.size, 0n, MAX_U64, true),
    modifiedNs: nullableDecimal(fields.modifiedNs, MIN_I64, MAX_I64),
    sourceState: fields.sourceState as SourceState,
    version: unsignedDecimal(fields.version, MAX_I64),
  };
}

const STATUS_KEYS = [
  "completedDirectories", "degradedDirectories", "generation", "lastCompletedAt",
  "observedEntries", "state", "updatedAt",
] as const;

function validatedIndexStatus(value: unknown): IndexStatus {
  const fields = objectWithKeys(value, STATUS_KEYS);
  if (typeof fields.state !== "string" || !statusStates.has(fields.state as IndexStatus["state"])) {
    invalidResponse();
  }
  return {
    state: fields.state as IndexStatus["state"],
    generation: unsignedDecimal(fields.generation, MAX_I64),
    observedEntries: unsignedDecimal(fields.observedEntries, MAX_I64),
    completedDirectories: unsignedDecimal(fields.completedDirectories, MAX_I64),
    degradedDirectories: unsignedDecimal(fields.degradedDirectories, MAX_I64),
    updatedAt: timestamp(fields.updatedAt),
    lastCompletedAt: timestamp(fields.lastCompletedAt),
  };
}

function validatedDirectoryPage(value: unknown, rootId: string, parentId: string | null): DirectoryPage {
  const fields = objectWithKeys(value, [
    "contractVersion", "directoryId", "directoryVersion", "entries", "indexStatus",
    "nextCursor", "previousCursor",
  ]);
  if (!Array.isArray(fields.entries) || fields.entries.length > 250 || fields.contractVersion !== 1) {
    invalidResponse();
  }
  const entries = fields.entries.map((entry) => validatedSummary(entry, rootId));
  if (new Set(entries.map((entry) => entry.id)).size !== entries.length) invalidResponse();
  const directoryId = uuid(fields.directoryId);
  if (parentId !== null && directoryId !== parentId) invalidResponse();
  return {
    entries,
    nextCursor: cursor(fields.nextCursor),
    previousCursor: cursor(fields.previousCursor),
    directoryId,
    directoryVersion: unsignedDecimal(fields.directoryVersion, MAX_I64),
    contractVersion: 1,
    indexStatus: validatedIndexStatus(fields.indexStatus),
  };
}

function validatedEntryDetails(value: unknown): EntryDetails {
  const fields = objectWithKeys(value, [...SUMMARY_KEYS, "ancestors", "ancestorsTruncated", "parentId"]);
  const summaryFields = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, fields[key]]));
  const summary = validatedSummary(summaryFields);
  if (!Array.isArray(fields.ancestors) || fields.ancestors.length > 64 ||
      typeof fields.ancestorsTruncated !== "boolean") invalidResponse();
  const ancestors = fields.ancestors.map((ancestor) => {
    const item = objectWithKeys(ancestor, ["displayName", "id"]);
    return {id: uuid(item.id), displayName: label(item.displayName, true)};
  });
  if (new Set(ancestors.map((ancestor) => ancestor.id)).size !== ancestors.length) invalidResponse();
  return {
    ...summary,
    parentId: fields.parentId === null ? null : uuid(fields.parentId),
    ancestors,
    ancestorsTruncated: fields.ancestorsTruncated,
  };
}

function validFilterTimestamp(value: unknown): boolean {
  try {
    return timestamp(value) !== null;
  } catch {
    return false;
  }
}

function validatedFilters(value: FileFilters): FileFilters {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  const fields = value as Record<string, unknown>;
  const allowed = new Set(["v", "kind", "type", "size", "modified", "availability", "prefix"]);
  if (Object.keys(fields).length > 8 || Object.keys(fields).some((key) => !allowed.has(key)) || fields.v !== 1) {
    invalidResponse();
  }
  const selections: [unknown, ReadonlySet<string>][] = [
    [fields.kind, entryKinds], [fields.availability, sourceStates],
  ];
  for (const [selection, choices] of selections) {
    if (selection === undefined) continue;
    if (!Array.isArray(selection) || selection.length > 32 ||
        selection.some((item) => typeof item !== "string" || !choices.has(item))) invalidResponse();
  }
  if (fields.type !== undefined && (!Array.isArray(fields.type) || fields.type.length > 32 ||
      fields.type.some((item) => typeof item !== "string" ||
        (item !== "__unknown__" && !TYPE_HINT.test(item))))) invalidResponse();
  if (fields.prefix !== undefined && (typeof fields.prefix !== "string" ||
      encoder.encode(fields.prefix).byteLength > 2048)) invalidResponse();
  if (fields.size !== undefined) {
    const size = fields.size as Record<string, unknown>;
    if (!size || typeof size !== "object" || Array.isArray(size) || Object.keys(size).length < 1 ||
        Object.keys(size).some((key) => !["min", "max", "unknown"].includes(key))) invalidResponse();
    if (size.unknown === true) {
      if (Object.keys(size).length !== 1) invalidResponse();
    } else {
      if (size.unknown !== undefined || (size.min === undefined && size.max === undefined)) invalidResponse();
      if (size.min !== undefined) unsignedDecimal(size.min, MAX_U64);
      if (size.max !== undefined) unsignedDecimal(size.max, MAX_U64);
      if (size.min !== undefined && size.max !== undefined && BigInt(size.min as string) > BigInt(size.max as string)) invalidResponse();
    }
  }
  if (fields.modified !== undefined) {
    const modified = fields.modified as Record<string, unknown>;
    if (!modified || typeof modified !== "object" || Array.isArray(modified) || Object.keys(modified).length < 1 ||
        Object.keys(modified).some((key) => !["from", "before", "unknown"].includes(key))) invalidResponse();
    if (modified.unknown === true) {
      if (Object.keys(modified).length !== 1) invalidResponse();
    } else {
      if (modified.unknown !== undefined || (modified.from === undefined && modified.before === undefined) ||
          (modified.from !== undefined && !validFilterTimestamp(modified.from)) ||
          (modified.before !== undefined && !validFilterTimestamp(modified.before))) invalidResponse();
      const from = modified.from === undefined ? null : timestampNanoseconds(modified.from);
      const before = modified.before === undefined ? null : timestampNanoseconds(modified.before);
      if ((from !== null && (from < MIN_I64 || from > MAX_I64)) ||
          (before !== null && (before < MIN_I64 || before > MAX_I64)) ||
          (from !== null && before !== null && from >= before)) invalidResponse();
    }
  }
  if (encoder.encode(JSON.stringify(fields)).byteLength > MAX_FILTER_BYTES) invalidResponse();
  return value;
}

function assertInput(input: BrowseInput): void {
  const allowed = new Set(["namespace", "rootId", "rootEpoch", "parentId", "filters", "sort", "order", "cursor"]);
  if (!input || typeof input !== "object" || Array.isArray(input) ||
      Object.keys(input).some((key) => !allowed.has(key)) ||
      typeof input.namespace !== "string" || input.namespace.length < 1 || input.namespace.length > 128 ||
      !Number.isSafeInteger(input.rootEpoch) || input.rootEpoch < 0 ||
      !["name", "modified", "size"].includes(input.sort) || !["asc", "desc"].includes(input.order)) {
    invalidResponse();
  }
  uuid(input.rootId);
  if (input.parentId !== null) uuid(input.parentId);
  validatedFilters(input.filters);
  if (input.cursor !== undefined) cursor(input.cursor);
}

async function authorityBoundRequest<T>(
  request: Promise<T>,
  snapshot: PrivateStateSnapshot,
  signal?: AbortSignal,
  namespace = snapshot.namespace,
): Promise<T> {
  try {
    return await request;
  } catch (error) {
    assertAuthority(snapshot, signal, namespace);
    throw error;
  }
}

function assertAuthority(
  snapshot: PrivateStateSnapshot,
  signal?: AbortSignal,
  namespace = snapshot.namespace,
): void {
  if (signal?.aborted || !isSessionAccessOpen() || !isPrivateStateCurrent(snapshot, namespace)) {
    throw genericApiProblem();
  }
}

export async function fetchDirectory(input: BrowseInput, signal: AbortSignal): Promise<DirectoryPage> {
  assertInput(input);
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal, input.namespace);
  const parameters = new URLSearchParams({
    filters: JSON.stringify(input.filters), sort: input.sort, order: input.order, limit: "100",
  });
  if (input.parentId !== null) parameters.set("parent", input.parentId);
  if (input.cursor !== undefined && input.cursor !== null) parameters.set("cursor", input.cursor);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/roots/${input.rootId}/entries?${parameters.toString()}`, {
      signal, maxResponseBytes: FILE_RESPONSE_BYTES,
    }),
    snapshot,
    signal,
    input.namespace,
  );
  const result = validatedDirectoryPage(response, input.rootId, input.parentId);
  assertAuthority(snapshot, signal, input.namespace);
  return result;
}

export async function fetchEntry(id: string, signal: AbortSignal): Promise<EntryDetails> {
  uuid(id);
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/entries/${id}`, {signal, maxResponseBytes: FILE_RESPONSE_BYTES}),
    snapshot,
    signal,
  );
  const result = validatedEntryDetails(response);
  if (result.id !== id) invalidResponse();
  assertAuthority(snapshot, signal);
  return result;
}

export async function fetchIndexStatus(rootId: string, signal: AbortSignal): Promise<IndexStatus> {
  uuid(rootId);
  const snapshot = capturePrivateState();
  assertAuthority(snapshot, signal);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/roots/${rootId}/index-status`, {
      signal, maxResponseBytes: FILE_RESPONSE_BYTES,
    }),
    snapshot,
    signal,
  );
  const result = validatedIndexStatus(response);
  assertAuthority(snapshot, signal);
  return result;
}

export async function requestScan(
  rootId: string,
  requestId: string,
  csrfToken: string,
): Promise<{scanId: string}> {
  uuid(rootId);
  if (!REQUEST_ID.test(requestId) || typeof csrfToken !== "string" || csrfToken.length < 1 || csrfToken.length > 256) {
    invalidResponse();
  }
  const snapshot = capturePrivateState();
  assertAuthority(snapshot);
  const response = await authorityBoundRequest(
    apiRequest<unknown>(`/api/v1/roots/${rootId}/scans`, {
      method: "POST", csrfToken, headers: {"X-Request-ID": requestId},
      maxResponseBytes: FILE_RESPONSE_BYTES,
    }),
    snapshot,
  );
  const fields = objectWithKeys(response, ["scanId"]);
  const result = {scanId: uuid(fields.scanId)};
  assertAuthority(snapshot);
  return result;
}
