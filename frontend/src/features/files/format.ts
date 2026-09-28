const exactCount = new Intl.NumberFormat("en-US", {useGrouping: true});
const DECIMAL = /^-?(?:0|[1-9][0-9]*)$/;
const NANOS_PER_SECOND = 1_000_000_000n;
const BYTE_UNITS: readonly [bigint, string][] = [
  [1_000_000_000_000_000_000n, "EB"],
  [1_000_000_000_000_000n, "PB"],
  [1_000_000_000_000n, "TB"],
  [1_000_000_000n, "GB"],
  [1_000_000n, "MB"],
  [1_000n, "KB"],
];

/** Group an exact decimal string without converting it to an unsafe number. */
export function formatExactCount(value: string): string {
  if (!DECIMAL.test(value)) return value;
  return exactCount.format(BigInt(value));
}

/** A rounded-down decimal approximation computed with BigInt, or null below 1 KB. */
export function approximateBytes(value: string): string | null {
  if (!DECIMAL.test(value) || value.startsWith("-")) return null;
  const bytes = BigInt(value);
  for (const [unit, label] of BYTE_UNITS) {
    if (bytes >= unit) {
      const tenths = (bytes * 10n) / unit;
      return `${tenths / 10n}.${tenths % 10n} ${label}`;
    }
  }
  return null;
}

/** Split signed nanoseconds into an exact UTC ISO string and a whole-second Date. */
export function nanosecondsToInstant(value: string): {date: Date; utc: string} | null {
  if (!DECIMAL.test(value)) return null;
  const nanoseconds = BigInt(value);
  const fraction = ((nanoseconds % NANOS_PER_SECOND) + NANOS_PER_SECOND) % NANOS_PER_SECOND;
  const seconds = (nanoseconds - fraction) / NANOS_PER_SECOND;
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return null;
  const whole = date.toISOString().replace(/\.\d{3}Z$/, "");
  return {date, utc: `${whole}.${fraction.toString().padStart(9, "0")}Z`};
}

export function formatLocalDateTime(date: Date): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "long",
  }).format(date);
}

/** Format an offset-qualified server timestamp in the device's local time. */
export function formatInstant(value: string | null): string | null {
  if (value === null) return null;
  const milliseconds = Date.parse(value.replace(/(\.\d{3})\d+/, "$1"));
  if (Number.isNaN(milliseconds)) return null;
  return formatLocalDateTime(new Date(milliseconds));
}
