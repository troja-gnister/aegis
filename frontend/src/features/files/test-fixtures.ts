import type {EntrySummary} from "./types";

export const FIXTURE_ROOT_ID = "11111111-1111-4111-8111-111111111111";

export function fixtureEntry(index: number): EntrySummary {
  const first = index.toString(16).padStart(8, "0");
  const tail = index.toString(16).padStart(12, "0");
  return {
    id: `${first}-2222-4222-8222-${tail}`,
    rootId: FIXTURE_ROOT_ID,
    displayName: `Synthetic file ${index.toString().padStart(4, "0")}.jpg`,
    kind: "file",
    typeHint: "image",
    size: String(index * 1024),
    modifiedNs: String(1_800_000_000_000_000_000n + BigInt(index)),
    sourceState: "present",
    version: String(index + 1),
  };
}
