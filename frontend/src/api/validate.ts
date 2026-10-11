import {genericApiProblem} from "./problem";

/** Shared strict validators for successful API bodies. Any violation is a 502. */

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;

export function invalidResponse(): never {
  throw genericApiProblem(502);
}

export function objectWithKeys(value: unknown, expected: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  const fields = value as Record<string, unknown>;
  const keys = Object.keys(fields).sort();
  const sortedExpected = [...expected].sort();
  if (keys.length !== sortedExpected.length || keys.some((key, index) => key !== sortedExpected[index])) {
    invalidResponse();
  }
  return fields;
}

export function uuid(value: unknown): string {
  if (typeof value !== "string" || !UUID.test(value)) invalidResponse();
  return value;
}

/** An offset-qualified RFC 3339 timestamp with a real calendar date, or null. */
export function timestamp(value: unknown): string | null {
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
