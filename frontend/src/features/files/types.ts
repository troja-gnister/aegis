import type {EntryMedia} from "../media/types";

export type EntryKind = "directory" | "file" | "symlink" | "special";
/**
 * Where a listing looks: one folder's children, or entries matching the filters
 * anywhere in the root. Kept in memory with the filters, never in a URL.
 */
export type BrowseScope = "directory" | "root";
export type SourceState = "present" | "missing" | "inaccessible" | "unsupported";

export type FileFilters = {
  v: 1;
  kind?: EntryKind[];
  type?: string[];
  size?: {min?: string; max?: string; unknown?: true};
  modified?: {from?: string; before?: string; unknown?: true};
  availability?: SourceState[];
  prefix?: string;
  /** Name search: literal text anywhere in a name, letter case ignored (3–255 characters). */
  contains?: string;
};

export type EntrySummary = {
  id: string;
  rootId: string;
  displayName: string;
  kind: EntryKind;
  typeHint: string | null;
  size: string | null;
  modifiedNs: string | null;
  sourceState: SourceState;
  version: string;
  /** Media summary for photo and video files; null for every other entry. */
  media: EntryMedia | null;
  /** Root-wide results only: the folder holding the entry (an empty name is the root's top level). */
  location?: EntryLocation;
};

export type EntryLocation = {parentId: string; parentName: string};

export type IndexStatus = {
  state: "not_indexed" | "queued" | "scanning" | "ready" | "degraded" | "unavailable";
  generation: string;
  observedEntries: string;
  completedDirectories: string;
  degradedDirectories: string;
  updatedAt: string | null;
  lastCompletedAt: string | null;
};

export type DirectoryPage = {
  entries: EntrySummary[];
  nextCursor: string | null;
  previousCursor: string | null;
  directoryId: string;
  directoryVersion: string;
  contractVersion: 1;
  indexStatus: IndexStatus;
};

export type EntryDetails = EntrySummary & {
  /**
   * The original's same-origin attachment URL, for Download. Set only with EXPORT and
   * only for a regular file that is present with current folders; null otherwise.
   */
  downloadUrl: string | null;
  parentId: string | null;
  ancestors: {id: string; displayName: string}[];
  ancestorsTruncated: boolean;
};

export type BrowseInput = {
  namespace: string;
  rootId: string;
  rootEpoch: number;
  parentId: string | null;
  filters: FileFilters;
  sort: "name" | "modified" | "size";
  order: "asc" | "desc";
  cursor?: string | null;
  /** A `root` listing has no parent: `parentId` is then null. */
  scope: BrowseScope;
};
