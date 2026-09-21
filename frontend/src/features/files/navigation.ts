import {
  capturePrivateState,
  isPrivateStateCurrent,
  registerPrivateStateCleanup,
} from "../auth/cache";
import type {FileFilters} from "./types";

const MAX_NAVIGATION_RECORDS = 32;
const MAX_NAVIGATION_RECORD_BYTES = 8 * 1024;
const encoder = new TextEncoder();

export type FileNavigationRecord = {
  rootId: string;
  parentId: string | null;
  filters: FileFilters;
  sort: "name" | "modified" | "size";
  order: "asc" | "desc";
  cursor: string | null;
  visibleAnchorId: string | null;
  visibleAnchorOffset: number;
};

export type FileNavigation = {
  readonly size: number;
  remember(key: string, record: FileNavigationRecord): void;
  recall(key: string): FileNavigationRecord | undefined;
  clearRoot(rootId: string): void;
  clear(): void;
  dispose(): void;
};

const owners = new Set<{namespace: string; navigation: FileNavigation}>();

function clonedRecord(record: FileNavigationRecord): FileNavigationRecord {
  return {
    rootId: record.rootId,
    parentId: record.parentId,
    filters: JSON.parse(JSON.stringify(record.filters)) as FileFilters,
    sort: record.sort,
    order: record.order,
    cursor: record.cursor,
    visibleAnchorId: record.visibleAnchorId,
    visibleAnchorOffset: record.visibleAnchorOffset,
  };
}

export function clearFileNavigationRoot(namespace: string, rootId: string): void {
  for (const owner of owners) {
    if (owner.namespace === namespace) owner.navigation.clearRoot(rootId);
  }
}

export function createFileNavigation(namespace: string): FileNavigation {
  const records = new Map<string, FileNavigationRecord>();
  const authority = capturePrivateState();
  const isOwnerCurrent = () => isPrivateStateCurrent(authority, namespace);
  let disposed = false;
  const clear = () => records.clear();
  const unregisterCleanup = registerPrivateStateCleanup(clear);
  const owner = {namespace, navigation: undefined as unknown as FileNavigation};
  const navigation: FileNavigation = {
    get size() { return records.size; },
    remember(key, record) {
      if (disposed || !isOwnerCurrent()) return;
      const copy = clonedRecord(record);
      const storedBytes = encoder.encode(key).byteLength + encoder.encode(JSON.stringify(copy)).byteLength;
      if (storedBytes > MAX_NAVIGATION_RECORD_BYTES) {
        throw new Error("Navigation record exceeds 8192 bytes");
      }
      records.delete(key);
      records.set(key, copy);
      while (records.size > MAX_NAVIGATION_RECORDS) {
        const oldest = records.keys().next().value as string | undefined;
        if (oldest === undefined) break;
        records.delete(oldest);
      }
    },
    recall(key) {
      if (disposed || !isOwnerCurrent()) return undefined;
      const record = records.get(key);
      if (!record) return undefined;
      records.delete(key);
      records.set(key, record);
      return clonedRecord(record);
    },
    clearRoot(rootId) {
      if (!isOwnerCurrent()) return;
      for (const [key, record] of records) {
        if (record.rootId === rootId) records.delete(key);
      }
    },
    clear,
    dispose() {
      if (disposed) return;
      disposed = true;
      clear();
      unregisterCleanup();
      owners.delete(owner);
    },
  };
  owner.navigation = navigation;
  owners.add(owner);
  return navigation;
}
