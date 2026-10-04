// Only a line number from one of this directory's spec files survives sanitization;
// paths, messages and values never do.
const SPEC_FRAME = /[\\/]e2e[\\/][a-z0-9]+\.spec\.ts:([1-9][0-9]*):[0-9]+/;

export function specFailureLine(stack: string | undefined): number | undefined {
  const match = stack === undefined ? null : SPEC_FRAME.exec(stack);
  if (match === null) return undefined;
  const line = Number(match[1]);
  return Number.isSafeInteger(line) ? line : undefined;
}
