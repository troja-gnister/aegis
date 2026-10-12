import {useInfiniteQuery, useQueryClient} from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";
import {Link, useLocation, useNavigate, useParams} from "react-router";
import {ApiProblem} from "../../api/problem";
import {useAuthSession} from "../auth/session";
import {RootSectionNav} from "../media/RootSectionNav";
import {
  forgetViewerReturn,
  isReturnRetained,
  peekViewerReturn,
  releaseReturnRetention,
} from "../media/viewer-return";
import {MEDIA_KIND_LABEL} from "../media/classify";
import type {MediaViewerContext} from "../media/types";
import {viewerLocationState, viewerPath} from "../media/viewer-route";
import {useAuthorizedRoot} from "../roots/useAuthorizedRoot";
import {EntryDetailsPanel} from "./EntryDetailsPanel";
import {FilterChips, ScopeToggle} from "./FilterChips";
import {FilterPanel} from "./FilterPanel";
import {activeFilterFields, listingScope, SEARCH_HINT, withSearch} from "./filter-state";
import {IndexStatusPanel} from "./IndexStatusPanel";
import {
  createFileNavigation,
  type FileNavigation,
  type FileNavigationRecord,
} from "./navigation";
import {NameSearch} from "./NameSearch";
import {directoryQueryOptions} from "./queries";
import type {BrowseInput, BrowseScope, EntrySummary, FileFilters, IndexStatus} from "./types";
import {ACTIVE_STATUS_INTERVAL} from "./useIndexStatus";
import {VirtualFileList, type VisibleAnchor} from "./VirtualFileList";
import "./files.css";

type FileNavigationRouteContext = {
  navigation: FileNavigation | null;
  revision: number;
  rememberRoute(key: string, record: FileNavigationRecord): void;
};

const FileNavigationContext = createContext<FileNavigationRouteContext | null>(null);

export function FileNavigationProvider({children}: PropsWithChildren) {
  const {session} = useAuthSession();
  const [owner, setOwner] = useState<{namespace: string; navigation: FileNavigation} | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const navigation = createFileNavigation(session.cacheNamespace);
    setOwner({namespace: session.cacheNamespace, navigation});
    return () => navigation.dispose();
  }, [session.cacheNamespace]);

  const navigation = owner?.namespace === session.cacheNamespace ? owner.navigation : null;
  const rememberRoute = useCallback((key: string, record: FileNavigationRecord) => {
    if (!navigation) return;
    navigation.remember(key, record);
    setRevision((current) => current + 1);
  }, [navigation]);
  const value = useMemo(() => ({navigation, revision, rememberRoute}), [navigation, rememberRoute, revision]);
  return <FileNavigationContext.Provider value={value}>{children}</FileNavigationContext.Provider>;
}

function useFileNavigationRoute() {
  const value = useContext(FileNavigationContext);
  if (!value) throw new Error("File navigation context is unavailable");
  return value;
}

export function FileNavigationRouteConsumer({children}: {
  children(value: FileNavigationRouteContext): ReactNode;
}) {
  return children(useFileNavigationRoute());
}

function statusMessage(state: string): string | null {
  if (state === "degraded") return "Some files could not be indexed.";
  if (state === "unavailable") return "This location is unavailable right now. Showing last indexed metadata.";
  if (state === "queued" || state === "scanning") return "Indexing this location…";
  return null;
}

/** Empty-window text; a zero-match filter result never reads as an unindexed directory. */
function emptyMessage(state: string | undefined, filters: FileFilters): string | null {
  if (state === "not_indexed") return "This location has not been indexed yet.";
  if (state === "queued" || state === "scanning") return null;
  const filtered = activeFilterFields(filters);
  if (filtered.length > 0 && (state === "ready" || state === "degraded" || state === "unavailable")) {
    if (filters.contains === undefined) return "No entries match these filters.";
    return `No files match “${filters.contains}”${filtered.length > 1 ? " with these filters" : ""}.`;
  }
  if (state === "ready") return "No files in this location.";
  return null;
}

function progressKey(status: IndexStatus): string {
  return [
    status.state, status.generation, status.observedEntries, status.completedDirectories,
    status.degradedDirectories, status.lastCompletedAt ?? "",
  ].join("|");
}

type Selection = {namespace: string; routeKey: string; id: string};

/** A root-wide result the viewer can show directly: a present media file, with PREVIEW. */
function opensInViewer(entry: EntrySummary, canPreview: boolean): boolean {
  return canPreview && entry.kind === "file" && entry.media !== null && entry.sourceState === "present";
}

/** The server refused a cursor from an older catalog view; only a page-one restart can continue. */
function isCursorRestart(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 409 && error.type === "cursor_restart_required";
}

/** Search text the server's trigram index cannot look up (the box checks only roughly). */
function isSearchTooShort(error: unknown): boolean {
  return error instanceof ApiProblem && error.status === 400 && error.type === "search_too_short";
}

export function FilesPage() {
  const {rootId = "", parentId} = useParams();
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const navigationRoute = useFileNavigationRoute();
  const navigation = navigationRoute.navigation;
  const [selection, setSelection] = useState<Selection | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [liveStatus, setLiveStatus] = useState<{rootId: string; status: IndexStatus} | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [resetRequest, setResetRequest] = useState(0);
  const anchorRef = useRef<VisibleAnchor>({id: null, offset: 0});
  const filtersButtonRef = useRef<HTMLButtonElement>(null);
  const filtersWereOpenRef = useRef(false);
  const detailsReturnRef = useRef<HTMLElement | null>(null);
  const announceResultRef = useRef<"filters" | "search" | "restart" | null>(null);
  const discardedWindowRef = useRef<unknown>(undefined);
  const lastProgressRef = useRef<string | null>(null);
  const lastProgressRetryRef = useRef(0);
  const routeKey = location.key;
  const restored = useMemo(
    () => navigation?.recall(routeKey),
    [navigation, navigationRoute.revision, routeKey],
  );
  const routeRecord = restored?.rootId === rootId && restored.parentId === (parentId ?? null)
    ? restored
    : undefined;
  const filters = routeRecord?.filters ?? {v: 1 as const};
  // Root-wide results need filters; without any, a route always lists its own folder.
  const scope = listingScope(filters, routeRecord?.scope);
  const sort = restored?.sort ?? "name";
  const order = restored?.order ?? "asc";
  const {rootQuery, root, sessionExpiring} = useAuthorizedRoot(rootId);
  // A viewer closed through Back left this route's window held for us; take it back as is.
  const retainedReturn = useMemo(
    () => isReturnRetained(session.cacheNamespace, routeKey),
    [routeKey, session.cacheNamespace],
  );
  const input: BrowseInput = {
    namespace: session.cacheNamespace,
    rootId,
    rootEpoch: root?.authorizationEpoch ?? 0,
    parentId: scope === "root" ? null : parentId ?? null,
    filters,
    sort,
    order,
    cursor: restored?.cursor ?? null,
    scope,
  };
  const directoryQuery = useInfiniteQuery({
    ...directoryQueryOptions(input),
    enabled: Boolean(root),
    ...(retainedReturn ? {refetchOnMount: false} : {}),
  });

  useEffect(() => {
    // This page's observer now holds the window (it subscribed above).
    if (retainedReturn) releaseReturnRetention(session.cacheNamespace, routeKey);
  }, [retainedReturn, routeKey, session.cacheNamespace]);
  const entries = directoryQuery.data?.pages.flatMap((page) => page.entries) ?? [];
  const status = directoryQuery.data?.pages.at(-1)?.indexStatus;
  const filtersActive = activeFilterFields(filters).length > 0;
  const windowEmpty = !directoryQuery.isPending && entries.length === 0;
  const live = liveStatus?.rootId === rootId ? liveStatus.status : undefined;
  const selectedFileId = selection?.namespace === session.cacheNamespace && selection.routeKey === routeKey
    ? selection.id
    : null;
  const restartRequired = directoryQuery.isError && isCursorRestart(directoryQuery.error);
  const searchTooShort = directoryQuery.isError && isSearchTooShort(directoryQuery.error);
  const directoryKeyRef = useRef(directoryQueryOptions(input).queryKey);
  const refetchDirectory = directoryQuery.refetch;
  // A viewer opened from this route leaves the entry it showed last; Back focuses that row once.
  const viewerReturnId = useMemo(
    () => peekViewerReturn(session.cacheNamespace, routeKey)?.entryId ?? null,
    [routeKey, session.cacheNamespace],
  );
  const [consumedReturnKey, setConsumedReturnKey] = useState<string | null>(null);
  const initialFocusId = consumedReturnKey === routeKey ? null : viewerReturnId;

  const remember = useCallback((anchor = anchorRef.current) => {
    if (!navigation) return;
    const stored = navigation.recall(routeKey);
    if (
      stored && stored.rootId === rootId && stored.parentId === (parentId ?? null) &&
      (JSON.stringify(stored.filters) !== JSON.stringify(filters) ||
        listingScope(stored.filters, stored.scope) !== scope ||
        stored.sort !== sort || stored.order !== order)
    ) {
      // A newer applied query identity owns this route; an obsolete closure must not revert it.
      return;
    }
    if (directoryQuery.data !== undefined && directoryQuery.data === discardedWindowRef.current) {
      // A page-one restart discarded this window; its cursors must not be written back.
      return;
    }
    const pages = directoryQuery.data?.pages ?? [];
    const anchorPageIndex = anchor.id === null
      ? -1
      : pages.findIndex((page) => page.entries.some((entry) => entry.id === anchor.id));
    const pageParam = directoryQuery.data?.pageParams[anchorPageIndex >= 0 ? anchorPageIndex : 0];
    navigation.remember(routeKey, {
      rootId,
      parentId: parentId ?? null,
      filters,
      scope,
      sort,
      order,
      cursor: typeof pageParam === "string" ? pageParam : null,
      visibleAnchorId: anchor.id,
      visibleAnchorOffset: anchor.offset,
    });
  }, [directoryQuery.data, filters, navigation, order, parentId, rootId, routeKey, scope, sort]);

  useEffect(() => () => remember(), [remember]);

  useEffect(() => {
    // Any route change (directory open, Back/Forward) evicts the selection; returning never revives it.
    setSelection(null);
  }, [routeKey]);

  useEffect(() => {
    directoryKeyRef.current = directoryQueryOptions(input).queryKey;
  });

  useEffect(() => {
    // The list captured the focus target when it mounted with entries; forget the record.
    if (viewerReturnId === null || entries.length === 0 || consumedReturnKey === routeKey) return;
    forgetViewerReturn(session.cacheNamespace, routeKey);
    setConsumedReturnKey(routeKey);
  }, [consumedReturnKey, entries.length, routeKey, session.cacheNamespace, viewerReturnId]);

  useEffect(() => {
    if (filtersWereOpenRef.current && !filtersOpen) filtersButtonRef.current?.focus({preventScroll: true});
    filtersWereOpenRef.current = filtersOpen;
  }, [filtersOpen]);

  useEffect(() => {
    // Re-applying identical filters (or a stale-cursor restart) still restarts at page one.
    if (resetRequest === 0) return;
    void queryClient.resetQueries({queryKey: directoryKeyRef.current, exact: true});
  }, [queryClient, resetRequest]);

  const searching = filters.contains !== undefined;
  const emptyText = windowEmpty ? emptyMessage(status?.state, filters) : null;
  useEffect(() => {
    const reason = announceResultRef.current;
    if (!reason || directoryQuery.isPending) return;
    announceResultRef.current = null;
    if (directoryQuery.isError) {
      setAnnouncement(searchTooShort
        ? `${SEARCH_HINT} to search.`
        : reason === "restart" ? "Files could not be loaded." : "Files could not be loaded for these filters.");
      return;
    }
    if (reason === "restart") {
      setAnnouncement(emptyText ?? "Showing files from the beginning.");
      return;
    }
    if (reason === "search") {
      setAnnouncement(emptyText ?? (searching ? "Search results updated." : "Search cleared. Results updated."));
      return;
    }
    setAnnouncement(emptyText ?? (filtersActive ? "Filters applied. Results updated." : "Filters cleared. Results updated."));
  }, [directoryQuery.isError, directoryQuery.isPending, emptyText, filtersActive, searchTooShort, searching]);

  useEffect(() => {
    // Progress while the first page is empty or failed retries it, at most once per active interval.
    if (!live) return;
    // The first observation for a root only records its baseline.
    const key = `${rootId}|${progressKey(live)}`;
    const previous = lastProgressRef.current?.startsWith(`${rootId}|`) ? lastProgressRef.current : null;
    lastProgressRef.current = key;
    if (previous === null || previous === key || !windowEmpty) return;
    const retry = () => {
      lastProgressRetryRef.current = Date.now();
      void refetchDirectory();
    };
    const wait = ACTIVE_STATUS_INTERVAL - (Date.now() - lastProgressRetryRef.current);
    if (wait <= 0) {
      retry();
      return;
    }
    const timer = setTimeout(retry, wait);
    return () => clearTimeout(timer);
  }, [live, refetchDirectory, rootId, windowEmpty]);

  const handleStatus = useCallback((value: IndexStatus) => {
    setLiveStatus({rootId, status: value});
  }, [rootId]);

  /** Records page one for this route under `next` filters; the caller resets the query when its key is unchanged. */
  const rememberPageOne = (next: FileFilters, nextScope: BrowseScope) => {
    anchorRef.current = {id: null, offset: 0};
    navigationRoute.rememberRoute(routeKey, {
      rootId,
      parentId: parentId ?? null,
      filters: next,
      scope: listingScope(next, nextScope),
      sort,
      order,
      cursor: null,
      visibleAnchorId: null,
      visibleAnchorOffset: 0,
    });
  };

  /** Resets the unchanged query key to page one and retires the window it replaces. */
  const restartWindow = () => {
    discardedWindowRef.current = directoryQuery.data;
    setResetRequest((count) => count + 1);
  };

  const applyFilters = (next: FileFilters, nextScope: BrowseScope) => {
    setFiltersOpen(false);
    setSelection(null);
    if (!navigation) return;
    const unchanged = JSON.stringify(next) === JSON.stringify(filters) &&
      listingScope(next, nextScope) === scope;
    announceResultRef.current = "filters";
    setAnnouncement(activeFilterFields(next).length > 0 ? "Applying filters…" : "Clearing filters…");
    rememberPageOne(next, nextScope);
    if (unchanged) restartWindow();
  };

  /** Like filters, a search looks in every folder unless this folder was chosen for them. */
  const applySearch = (text: string) => {
    applyFilters(withSearch(filters, text), filtersActive ? scope : "root");
    announceResultRef.current = "search";
    setAnnouncement(text === "" ? "Clearing search…" : "Searching…");
  };

  /** A stale cursor cannot be retried; drop every retained cursor and reload page one of the same query. */
  const restartFromBeginning = () => {
    setSelection(null);
    if (!navigation) return;
    announceResultRef.current = "restart";
    setAnnouncement("Starting from the beginning…");
    rememberPageOne(filters, scope);
    restartWindow();
    filtersButtonRef.current?.focus({preventScroll: true});
  };

  if (rootQuery.isPending || sessionExpiring) {
    return <section className="files-page"><p role="status">Loading root…</p></section>;
  }
  if (rootQuery.isError) {
    return (
      <section className="files-page">
        <h1>Files unavailable</h1>
        <p className="notice notice--error" role="alert">The authorized root could not be loaded.</p>
      </section>
    );
  }
  if (!root) {
    return (
      <section className="files-page files-page--empty">
        <p className="eyebrow">Library</p>
        <h1>Root unavailable</h1>
        <p>This root is not assigned to this account.</p>
        <Link className="file-page-control interactive" to="/roots">Return to roots</Link>
      </section>
    );
  }

  const empty = !directoryQuery.isError && windowEmpty ? emptyMessage(status?.state, filters) : null;
  const refreshAvailable = Boolean(
    live && status && entries.length > 0 && !directoryQuery.isFetching &&
      (live.generation !== status.generation || live.lastCompletedAt !== status.lastCompletedAt),
  );
  const announcementTestId = import.meta.env.MODE === "test" ? {"data-testid": "files-announcement"} : {};
  const canPreview = root.permissions.includes("preview");
  // History state carries opaque IDs and the route key only; filters stay in the navigation record.
  const viewerContext: MediaViewerContext = {
    source: scope === "root" ? "search" : "directory",
    rootId: root.id,
    parentId: parentId ?? null,
    returnKey: routeKey,
  };
  const folderName = (entry: EntrySummary) => entry.location === undefined
    ? null
    : entry.location.parentName === "" ? root.displayName : entry.location.parentName;
  const selectedLocation = selectedFileId === null
    ? undefined
    : entries.find((entry) => entry.id === selectedFileId)?.location;

  return (
    <section className="files-page">
      <nav className="files-page__breadcrumbs" aria-label="File location">
        <Link className="interactive" to="/roots">Roots</Link>
        {parentId ? <Link className="interactive" to={`/files/${root.id}`}>{root.displayName}</Link> : null}
      </nav>
      <p className="eyebrow">Read-only library</p>
      <h1 dir="auto">{root.displayName}</h1>
      <RootSectionNav rootId={root.id} current="files" />
      <p className="files-page__access">Original access: read only</p>
      <IndexStatusPanel
        key={`${session.cacheNamespace}:${root.id}:${root.authorizationEpoch}`}
        rootId={root.id}
        rootEpoch={root.authorizationEpoch}
        canRequestScan={root.permissions.includes("root_admin")}
        onStatus={handleStatus}
      />
      <div className="files-toolbar">
        <NameSearch applied={filters.contains} onSearch={applySearch} />
        <button
          ref={filtersButtonRef}
          className="file-page-control interactive"
          type="button"
          aria-haspopup="dialog"
          aria-expanded={filtersOpen}
          onClick={() => setFiltersOpen(true)}
        >
          Filters
        </button>
        {filtersActive ? (
          <ScopeToggle scope={scope} onChange={(next) => applyFilters(filters, next)} />
        ) : null}
        <FilterChips
          value={filters}
          onChange={(next) => {
            applyFilters(next, scope);
            filtersButtonRef.current?.focus({preventScroll: true});
          }}
        />
      </div>
      <p className="visually-hidden" role="status" aria-live="polite" {...announcementTestId}>
        {announcement}
      </p>
      {directoryQuery.isPending ? (
        <p role="status">{searching ? "Searching…" : "Loading files…"}</p>
      ) : null}
      {restartRequired ? (
        <div className="files-page__restart">
          <p className="notice notice--error" role="alert">
            This file list changed. Start from the beginning to keep browsing.
          </p>
          <button className="file-page-control interactive" type="button" onClick={restartFromBeginning}>
            Start from the beginning
          </button>
        </div>
      ) : searchTooShort ? (
        <p className="notice notice--error" role="alert">
          {SEARCH_HINT} to search.
        </p>
      ) : directoryQuery.isError ? (
        <p className="notice notice--error" role="alert">Files could not be loaded. Please try again.</p>
      ) : null}
      {statusMessage(status?.state ?? "") ? (
        <p
          className={status?.state === "degraded" || status?.state === "unavailable"
            ? "notice notice--warning"
            : "notice"}
          role="status"
        >
          {statusMessage(status?.state ?? "")}
        </p>
      ) : null}
      {empty ? <p className="files-page__empty">{empty}</p> : null}
      {refreshAvailable ? (
        <div className="files-page__refresh">
          <p>The index changed since these files loaded.</p>
          <button className="file-page-control interactive" type="button" onClick={() => void refetchDirectory()}>
            Refresh files
          </button>
        </div>
      ) : null}
      {entries.length > 0 ? (
        <VirtualFileList
          // A page-one restart starts a fresh window; it never carries the old scroll position over.
          key={resetRequest}
          entries={entries}
          onOpen={(entry) => {
            if (entry.kind === "directory") {
              remember();
              setSelection(null);
              navigate(`/files/${root.id}/directories/${entry.id}`);
            } else if (scope === "root" && opensInViewer(entry, canPreview)) {
              // The viewer moves through these results; Back returns to this window and row.
              remember();
              setSelection(null);
              navigate(viewerPath(root.id, entry.id), {state: viewerLocationState(entry.id, viewerContext)});
            } else {
              detailsReturnRef.current = document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
              setSelection({namespace: session.cacheNamespace, routeKey, id: entry.id});
            }
          }}
          onLoadNext={() => directoryQuery.fetchNextPage()}
          onLoadPrevious={() => directoryQuery.fetchPreviousPage()}
          hasNext={Boolean(directoryQuery.hasNextPage) && !restartRequired}
          hasPrevious={Boolean(directoryQuery.hasPreviousPage) && !restartRequired}
          initialAnchor={restored ? {
            id: restored.visibleAnchorId,
            offset: restored.visibleAnchorOffset,
          } : undefined}
          onAnchorChange={(anchor) => {
            anchorRef.current = anchor;
            remember(anchor);
          }}
          initialFocusId={initialFocusId}
          locationOf={scope === "root" ? folderName : undefined}
          actionOf={scope === "root"
            ? (entry) => opensInViewer(entry, canPreview) && entry.media !== null
              ? `Open ${MEDIA_KIND_LABEL[entry.media.kind].toLowerCase()}`
              : undefined
            : undefined}
        />
      ) : null}
      {selectedFileId ? (
        <EntryDetailsPanel
          key={selectedFileId}
          entryId={selectedFileId}
          viewerContext={viewerContext}
          // The viewer's Back returns here; record the visible anchor like a directory open does.
          onOpenMedia={() => remember()}
          folder={selectedLocation === undefined ? undefined : {
            to: selectedLocation.parentName === ""
              ? `/files/${root.id}`
              : `/files/${root.id}/directories/${selectedLocation.parentId}`,
            name: selectedLocation.parentName === "" ? root.displayName : selectedLocation.parentName,
            onOpen: () => remember(),
          }}
          onClose={() => {
            const target = detailsReturnRef.current;
            setSelection(null);
            if (target?.isConnected) target.focus({preventScroll: true});
          }}
        />
      ) : null}
      <FilterPanel
        value={filters}
        scope={scope}
        open={filtersOpen}
        onApply={applyFilters}
        onCancel={() => setFiltersOpen(false)}
      />
    </section>
  );
}
