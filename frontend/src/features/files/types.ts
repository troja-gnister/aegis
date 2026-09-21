export type EntryKind = "directory" | "file" | "symlink" | "special";
export type SourceState = "present" | "missing" | "inaccessible" | "unsupported";

export type FileFilters = {
  v: 1;
  kind?: EntryKind[];
  type?: string[];
  size?: {min?: string; max?: string; unknown?: true};
  modified?: {from?: string; before?: string; unknown?: true};
  availability?: SourceState[];
  prefix?: string;
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
};

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
};
