import {formatExactCount} from "./format";
import type {EntryKind, FileFilters, SourceState} from "./types";

export type FilterField = keyof Omit<FileFilters, "v">;
export type SizeUnit = "B" | "KB" | "MB" | "GB" | "TB";

export const UNKNOWN_TYPE = "__unknown__";
export const MAX_FIELD_SELECTIONS = 32;
const MAX_PREFIX_BYTES = 2048;
const MAX_FILTER_BYTES = 8192;
const MAX_SIZE = 18_446_744_073_709_551_615n;
const MIN_TIMESTAMP_NS = -9_223_372_036_854_775_808n;
const MAX_TIMESTAMP_NS = 9_223_372_036_854_775_807n;
const TYPE_HINT = /^[a-z0-9]{1,16}$/;
const LOCAL_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const LOCAL_DAY_START = /^(\d{4}-\d{2}-\d{2})T00:00:00([+-]\d{2}:\d{2})$/;
const encoder = new TextEncoder();

export const FILTER_FIELDS: readonly FilterField[] = [
  "kind", "type", "size", "modified", "availability", "prefix",
];

export const KIND_OPTIONS: readonly {value: EntryKind; label: string}[] = [
  {value: "directory", label: "Directory"},
  {value: "file", label: "File"},
  {value: "symlink", label: "Symbolic link"},
  {value: "special", label: "Special entry"},
];

export const AVAILABILITY_OPTIONS: readonly {value: SourceState; label: string}[] = [
  {value: "present", label: "Present"},
  {value: "missing", label: "Missing"},
  {value: "inaccessible", label: "Inaccessible"},
  {value: "unsupported", label: "Unsupported"},
];

/** Static common extensions; no facet query or count is needed to offer them. */
export const COMMON_TYPES: readonly string[] = [
  "jpg", "jpeg", "png", "heic", "gif", "webp", "mp4", "mov", "pdf", "txt", "docx", "zip",
];

export const SIZE_UNITS: readonly {value: SizeUnit; label: string; digits: number}[] = [
  {value: "B", label: "bytes", digits: 0},
  {value: "KB", label: "KB (1,000 bytes)", digits: 3},
  {value: "MB", label: "MB (1,000,000 bytes)", digits: 6},
  {value: "GB", label: "GB (10⁹ bytes)", digits: 9},
  {value: "TB", label: "TB (10¹² bytes)", digits: 12},
];

export type FilterDraft = {
  kind: EntryKind[];
  type: string[];
  extraTypes: string;
  availability: SourceState[];
  prefix: string;
  sizeMin: string;
  sizeMinUnit: SizeUnit;
  sizeMax: string;
  sizeMaxUnit: SizeUnit;
  sizeUnknown: boolean;
  modifiedFrom: string;
  modifiedTo: string;
  modifiedUnknown: boolean;
};

export type FilterDraftErrors = Partial<Record<FilterField | "form", string>>;
export type FilterDraftResult =
  | {ok: true; filters: FileFilters}
  | {ok: false; errors: FilterDraftErrors};

export function removeFilter(
  filters: FileFilters,
  field: keyof Omit<FileFilters, "v">,
): FileFilters {
  const result = {...filters};
  delete result[field];
  return result;
}

export function activeFilterFields(filters: FileFilters): FilterField[] {
  return FILTER_FIELDS.filter((field) => filters[field] !== undefined);
}

export function emptyFilterDraft(): FilterDraft {
  return {
    kind: [],
    type: [],
    extraTypes: "",
    availability: [],
    prefix: "",
    sizeMin: "",
    sizeMinUnit: "B",
    sizeMax: "",
    sizeMaxUnit: "B",
    sizeUnknown: false,
    modifiedFrom: "",
    modifiedTo: "",
    modifiedUnknown: false,
  };
}

function isStaticType(value: string): boolean {
  return value === UNKNOWN_TYPE || COMMON_TYPES.includes(value);
}

function shiftLocalDate(date: string, days: number): string | null {
  const match = LOCAL_DATE.exec(date);
  if (!match) return null;
  const calendar = new Date(0);
  calendar.setUTCFullYear(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days);
  const year = String(calendar.getUTCFullYear()).padStart(4, "0");
  const month = String(calendar.getUTCMonth() + 1).padStart(2, "0");
  const day = String(calendar.getUTCDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Build a fresh, bounded draft from the applied filters. */
export function draftFromFilters(filters: FileFilters): FilterDraft {
  const draft = emptyFilterDraft();
  draft.kind = KIND_OPTIONS.map((option) => option.value)
    .filter((kind) => filters.kind?.includes(kind));
  draft.availability = AVAILABILITY_OPTIONS.map((option) => option.value)
    .filter((state) => filters.availability?.includes(state));
  const types = (filters.type ?? []).slice(0, MAX_FIELD_SELECTIONS);
  draft.type = types.filter(isStaticType);
  draft.extraTypes = types.filter((type) => !isStaticType(type)).join(", ");
  draft.prefix = filters.prefix ?? "";
  if (filters.size?.unknown) draft.sizeUnknown = true;
  else {
    draft.sizeMin = filters.size?.min ?? "";
    draft.sizeMax = filters.size?.max ?? "";
  }
  if (filters.modified?.unknown) draft.modifiedUnknown = true;
  else {
    const from = filters.modified?.from ? LOCAL_DAY_START.exec(filters.modified.from) : null;
    const before = filters.modified?.before ? LOCAL_DAY_START.exec(filters.modified.before) : null;
    draft.modifiedFrom = from?.[1] ?? "";
    draft.modifiedTo = before ? shiftLocalDate(before[1]!, -1) ?? "" : "";
  }
  return draft;
}

/** Convert an exact decimal quantity in a decimal unit to whole bytes, or null. */
export function parseSizeBytes(value: string, unit: SizeUnit): string | null {
  const text = value.trim();
  const match = /^(\d{1,24})(?:\.(\d{1,24}))?$/.exec(text);
  if (!match) return null;
  const digits = SIZE_UNITS.find((candidate) => candidate.value === unit)?.digits ?? 0;
  const fraction = match[2] ?? "";
  if (fraction.slice(digits).replace(/0/g, "") !== "") return null;
  const bytes = BigInt(`${match[1]}${fraction.slice(0, digits).padEnd(digits, "0")}`);
  return bytes <= MAX_SIZE ? bytes.toString() : null;
}

function formatOffset(minutesEast: number): string {
  const sign = minutesEast < 0 ? "-" : "+";
  const absolute = Math.abs(minutesEast);
  const hours = String(Math.floor(absolute / 60)).padStart(2, "0");
  const minutes = String(absolute % 60).padStart(2, "0");
  return `${sign}${hours}:${minutes}`;
}

/**
 * Map a local calendar day to its offset-qualified start instant. Returns null
 * for malformed dates and for a local midnight that does not exist (for
 * example a daylight-saving gap), rather than silently shifting it.
 */
export function localDayStart(date: string): string | null {
  const match = LOCAL_DATE.exec(date);
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const local = new Date(2000, 0, 1, 0, 0, 0, 0);
  local.setFullYear(year, month - 1, day);
  local.setHours(0, 0, 0, 0);
  if (
    local.getFullYear() !== year || local.getMonth() !== month - 1 || local.getDate() !== day ||
    local.getHours() !== 0 || local.getMinutes() !== 0 || local.getSeconds() !== 0
  ) return null;
  const offset = -local.getTimezoneOffset();
  if (!Number.isInteger(offset)) return null;
  return `${match[1]}-${match[2]}-${match[3]}T00:00:00${formatOffset(offset)}`;
}

function instantNanoseconds(value: string): bigint {
  const milliseconds = Date.parse(value);
  return BigInt(milliseconds) * 1_000_000n;
}

export function localTimeZoneName(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "device local time";
  } catch {
    return "device local time";
  }
}

/** Byte length of the compact ASCII-escaped JSON the server budgets. */
function asciiJsonBytes(value: unknown): number {
  let total = 0;
  for (const character of JSON.stringify(value)) {
    const point = character.codePointAt(0)!;
    total += point > 0xffff ? 12 : point > 0x7e ? 6 : 1;
  }
  return total;
}

function parseExtraTypes(text: string): {types: string[]; invalid: boolean} {
  const tokens = text.split(/[\s,]+/).filter(Boolean);
  const types: string[] = [];
  let invalid = false;
  for (const token of tokens) {
    const normalized = token.replace(/^\./, "").toLowerCase();
    if (!TYPE_HINT.test(normalized)) invalid = true;
    else types.push(normalized);
  }
  return {types, invalid};
}

function uniqueInOrder<T>(values: T[]): T[] {
  return [...new Set(values)];
}

/** Validate a draft once and produce the bounded wire filters. */
export function filtersFromDraft(draft: FilterDraft): FilterDraftResult {
  const errors: FilterDraftErrors = {};
  const filters: FileFilters = {v: 1};

  const kind = KIND_OPTIONS.map((option) => option.value).filter((value) => draft.kind.includes(value));
  if (kind.length > 0) filters.kind = kind;

  const extra = parseExtraTypes(draft.extraTypes);
  const type = uniqueInOrder([
    ...COMMON_TYPES.filter((value) => draft.type.includes(value)),
    ...(draft.type.includes(UNKNOWN_TYPE) ? [UNKNOWN_TYPE] : []),
    ...extra.types,
  ]);
  if (extra.invalid) {
    errors.type = "Extensions use 1–16 letters or digits, for example jpg. Use “No extension” for unknown types.";
  } else if (type.length > MAX_FIELD_SELECTIONS) {
    errors.type = `Select at most ${MAX_FIELD_SELECTIONS} file types.`;
  } else if (type.length > 0) filters.type = type;

  const availability = AVAILABILITY_OPTIONS.map((option) => option.value)
    .filter((value) => draft.availability.includes(value));
  if (availability.length > 0) filters.availability = availability;

  if (draft.prefix !== "") {
    if (encoder.encode(draft.prefix).byteLength > MAX_PREFIX_BYTES) {
      errors.prefix = "The filename prefix is too long.";
    } else filters.prefix = draft.prefix;
  }

  const hasSizeRange = draft.sizeMin.trim() !== "" || draft.sizeMax.trim() !== "";
  if (draft.sizeUnknown && hasSizeRange) {
    errors.size = "Choose either unknown size or a size range, not both.";
  } else if (draft.sizeUnknown) {
    filters.size = {unknown: true};
  } else if (hasSizeRange) {
    const min = draft.sizeMin.trim() === "" ? undefined : parseSizeBytes(draft.sizeMin, draft.sizeMinUnit);
    const max = draft.sizeMax.trim() === "" ? undefined : parseSizeBytes(draft.sizeMax, draft.sizeMaxUnit);
    if (min === null || max === null) {
      errors.size = "Enter a size as a decimal number that is a whole number of bytes.";
    } else if (min !== undefined && max !== undefined && BigInt(min) > BigInt(max)) {
      errors.size = "The minimum size must not exceed the maximum size.";
    } else {
      filters.size = {
        ...(min === undefined ? {} : {min}),
        ...(max === undefined ? {} : {max}),
      };
    }
  }

  const hasDateRange = draft.modifiedFrom !== "" || draft.modifiedTo !== "";
  if (draft.modifiedUnknown && hasDateRange) {
    errors.modified = "Choose either unknown modified time or a date range, not both.";
  } else if (draft.modifiedUnknown) {
    filters.modified = {unknown: true};
  } else if (hasDateRange) {
    const zone = localTimeZoneName();
    const validCalendarDate = (date: string) => LOCAL_DATE.test(date) && shiftLocalDate(date, 0) === date;
    const dayAfter = validCalendarDate(draft.modifiedTo) ? shiftLocalDate(draft.modifiedTo, 1) : null;
    const from = draft.modifiedFrom === "" ? undefined : localDayStart(draft.modifiedFrom);
    const before = draft.modifiedTo === "" ? undefined : dayAfter === null ? null : localDayStart(dayAfter);
    if (from === null) {
      errors.modified = validCalendarDate(draft.modifiedFrom)
        ? `Local midnight does not exist on ${draft.modifiedFrom} in ${zone}. Choose another date.`
        : "Enter a valid start date.";
    } else if (before === null) {
      errors.modified = dayAfter !== null
        ? `The day after ${draft.modifiedTo} has no local midnight in ${zone}. Choose another end date.`
        : "Enter a valid end date.";
    } else {
      const fromNs = from === undefined ? null : instantNanoseconds(from);
      const beforeNs = before === undefined ? null : instantNanoseconds(before);
      const outOfRange = [fromNs, beforeNs].some((value) =>
        value !== null && (value < MIN_TIMESTAMP_NS || value > MAX_TIMESTAMP_NS));
      if (outOfRange) {
        errors.modified = "Choose dates between 1678 and 2261.";
      } else if (fromNs !== null && beforeNs !== null && fromNs >= beforeNs) {
        errors.modified = "The end date must be on or after the start date.";
      } else {
        filters.modified = {
          ...(from === undefined ? {} : {from}),
          ...(before === undefined ? {} : {before}),
        };
      }
    }
  }

  if (Object.keys(errors).length === 0 && asciiJsonBytes(filters) > MAX_FILTER_BYTES) {
    errors.form = "These filters are too large. Remove some values and try again.";
  }
  return Object.keys(errors).length > 0 ? {ok: false, errors} : {ok: true, filters};
}

function joinOr(values: string[]): string {
  return values.join(" or ");
}

function typeLabel(value: string): string {
  return value === UNKNOWN_TYPE ? "no extension" : `.${value}`;
}

function instantLabel(value: string): string {
  const match = LOCAL_DAY_START.exec(value);
  if (match) return `${match[1]} 00:00 UTC${match[2]}`;
  return value;
}

/** A complete human-readable label for one applied filter group. */
export function describeFilterField(filters: FileFilters, field: FilterField): string {
  switch (field) {
    case "kind":
      return `Kind: ${joinOr((filters.kind ?? []).map((value) =>
        KIND_OPTIONS.find((option) => option.value === value)?.label ?? value))}`;
    case "type":
      return `Type: ${joinOr((filters.type ?? []).map(typeLabel))}`;
    case "availability":
      return `Last indexed state: ${joinOr((filters.availability ?? []).map((value) =>
        AVAILABILITY_OPTIONS.find((option) => option.value === value)?.label ?? value))}`;
    case "prefix":
      return `Filename starts with "${filters.prefix ?? ""}"`;
    case "size": {
      const size = filters.size;
      if (!size || size.unknown) return "Size: unknown";
      if (size.min !== undefined && size.max !== undefined) {
        return `Size: ${formatExactCount(size.min)} to ${formatExactCount(size.max)} bytes`;
      }
      if (size.min !== undefined) return `Size: at least ${formatExactCount(size.min)} bytes`;
      return `Size: at most ${formatExactCount(size.max ?? "0")} bytes`;
    }
    case "modified": {
      const modified = filters.modified;
      if (!modified || modified.unknown) return "Modified: unknown";
      const parts: string[] = [];
      if (modified.from !== undefined) parts.push(`from ${instantLabel(modified.from)}`);
      if (modified.before !== undefined) {
        parts.push(`${modified.from === undefined ? "" : "until "}before ${instantLabel(modified.before)}`);
      }
      return `Modified: ${parts.join(" ")}`;
    }
  }
}
