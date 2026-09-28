import {useInfiniteQuery, useQuery, useQueryClient} from "@tanstack/react-query";
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
import {purgePrivateBrowserState} from "../auth/cache";
import {beginSignOut, completeSignOut, useAuthSession} from "../auth/session";
import {fetchRoots} from "../roots/api";
import {EntryDetailsPanel} from "./EntryDetailsPanel";
import {FilterChips} from "./FilterChips";
import {FilterPanel} from "./FilterPanel";
import {activeFilterFields} from "./filter-state";
import {IndexStatusPanel} from "./IndexStatusPanel";
import {
  createFileNavigation,
  type FileNavigation,
  type FileNavigationRecord,
} from "./navigation";
import {directoryQueryOptions} from "./queries";
import type {BrowseInput, FileFilters, IndexStatus} from "./types";
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
function emptyMessage(state: string | undefined, filtersActive: boolean): string | null {
  if (state === "not_indexed") return "This location has not been indexed yet.";
  if (state === "queued" || state === "scanning") return null;
  if (filtersActive && (state === "ready" || state === "degraded" || state === "unavailable")) {
    return "No entries match these filters.";
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

export function FilesPage() {
  const {rootId = "", parentId} = useParams();
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const navigationRoute = useFileNavigationRoute();
  const navigation = navigationRoute.navigation;
  const [sessionExpiring, setSessionExpiring] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [liveStatus, setLiveStatus] = useState<{rootId: string; status: IndexStatus} | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [resetRequest, setResetRequest] = useState(0);
  const anchorRef = useRef<VisibleAnchor>({id: null, offset: 0});
  const filtersButtonRef = useRef<HTMLButtonElement>(null);
  const filtersWereOpenRef = useRef(false);
  const detailsReturnRef = useRef<HTMLElement | null>(null);
  const announceResultRef = useRef(false);
  const lastProgressRef = useRef<string | null>(null);
  const lastProgressRetryRef = useRef(0);
  const routeKey = location.key;
  const restored = useMemo(
    () => navigation?.recall(routeKey),
    [navigation, navigationRoute.revision, routeKey],
  );
  const filters = restored?.rootId === rootId && restored.parentId === (parentId ?? null)
    ? restored.filters
    : {v: 1 as const};
  const sort = restored?.sort ?? "name";
  const order = restored?.order ?? "asc";
  const rootQuery = useQuery({
    queryKey: ["roots", session.cacheNamespace],
    queryFn: fetchRoots,
    retry: false,
  });
  const root = rootQuery.data?.roots.find((candidate) =>
    candidate.id === rootId && candidate.permissions.includes("browse"),
  );
  const input: BrowseInput = {
    namespace: session.cacheNamespace,
    rootId,
    rootEpoch: root?.authorizationEpoch ?? 0,
    parentId: parentId ?? null,
    filters,
    sort,
    order,
    cursor: restored?.cursor ?? null,
  };
  const directoryQuery = useInfiniteQuery({
    ...directoryQueryOptions(input),
    enabled: Boolean(root),
  });
  const entries = directoryQuery.data?.pages.flatMap((page) => page.entries) ?? [];
  const status = directoryQuery.data?.pages.at(-1)?.indexStatus;
  const filtersActive = activeFilterFields(filters).length > 0;
  const windowEmpty = !directoryQuery.isPending && entries.length === 0;
  const live = liveStatus?.rootId === rootId ? liveStatus.status : undefined;
  const selectedFileId = selection?.namespace === session.cacheNamespace && selection.routeKey === routeKey
    ? selection.id
    : null;
  const directoryKeyRef = useRef(directoryQueryOptions(input).queryKey);
  const refetchDirectory = directoryQuery.refetch;

  const remember = useCallback((anchor = anchorRef.current) => {
    if (!navigation) return;
    const stored = navigation.recall(routeKey);
    if (
      stored && stored.rootId === rootId && stored.parentId === (parentId ?? null) &&
      (JSON.stringify(stored.filters) !== JSON.stringify(filters) ||
        stored.sort !== sort || stored.order !== order)
    ) {
      // A newer applied query identity owns this route; an obsolete closure must not revert it.
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
      sort,
      order,
      cursor: typeof pageParam === "string" ? pageParam : null,
      visibleAnchorId: anchor.id,
      visibleAnchorOffset: anchor.offset,
    });
  }, [directoryQuery.data, filters, navigation, order, parentId, rootId, routeKey, sort]);

  useEffect(() => () => remember(), [remember]);

  useEffect(() => {
    // Any route change (directory open, Back/Forward) evicts the selection; returning never revives it.
    setSelection(null);
  }, [routeKey]);

  useEffect(() => {
    directoryKeyRef.current = directoryQueryOptions(input).queryKey;
  });

  useEffect(() => {
    if (filtersWereOpenRef.current && !filtersOpen) filtersButtonRef.current?.focus({preventScroll: true});
    filtersWereOpenRef.current = filtersOpen;
  }, [filtersOpen]);

  useEffect(() => {
    // Re-applying identical filters still restarts at page one.
    if (resetRequest === 0) return;
    void queryClient.resetQueries({queryKey: directoryKeyRef.current, exact: true});
  }, [queryClient, resetRequest]);

  useEffect(() => {
    if (!announceResultRef.current || directoryQuery.isPending) return;
    announceResultRef.current = false;
    if (directoryQuery.isError) {
      setAnnouncement("Files could not be loaded for these filters.");
      return;
    }
    const empty = windowEmpty ? emptyMessage(status?.state, filtersActive) : null;
    setAnnouncement(empty ?? (filtersActive ? "Filters applied. Results updated." : "Filters cleared. Results updated."));
  }, [directoryQuery.isError, directoryQuery.isPending, filtersActive, status?.state, windowEmpty]);

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

  const applyFilters = (next: FileFilters) => {
    setFiltersOpen(false);
    setSelection(null);
    if (!navigation) return;
    const unchanged = JSON.stringify(next) === JSON.stringify(filters);
    anchorRef.current = {id: null, offset: 0};
    announceResultRef.current = true;
    setAnnouncement(activeFilterFields(next).length > 0 ? "Applying filters…" : "Clearing filters…");
    navigationRoute.rememberRoute(routeKey, {
      rootId,
      parentId: parentId ?? null,
      filters: next,
      sort,
      order,
      cursor: null,
      visibleAnchorId: null,
      visibleAnchorOffset: 0,
    });
    if (unchanged) setResetRequest((count) => count + 1);
  };

  useEffect(() => {
    if (!(rootQuery.error instanceof ApiProblem) || rootQuery.error.status !== 401 || sessionExpiring) return;
    setSessionExpiring(true);
    const generation = beginSignOut();
    void purgePrivateBrowserState(queryClient).then(
      () => completeSignOut(generation, true),
      () => completeSignOut(generation, true, false),
    ).then(() => navigate("/login", {replace: true, state: {reason: "session"}}));
  }, [navigate, queryClient, rootQuery.error, sessionExpiring]);

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

  const empty = !directoryQuery.isError && windowEmpty ? emptyMessage(status?.state, filtersActive) : null;
  const refreshAvailable = Boolean(
    live && status && entries.length > 0 && !directoryQuery.isFetching &&
      (live.generation !== status.generation || live.lastCompletedAt !== status.lastCompletedAt),
  );
  const announcementTestId = import.meta.env.MODE === "test" ? {"data-testid": "files-announcement"} : {};

  return (
    <section className="files-page">
      <nav className="files-page__breadcrumbs" aria-label="File location">
        <Link className="interactive" to="/roots">Roots</Link>
        {parentId ? <Link className="interactive" to={`/files/${root.id}`}>{root.displayName}</Link> : null}
      </nav>
      <p className="eyebrow">Read-only library</p>
      <h1 dir="auto">{root.displayName}</h1>
      <p className="files-page__access">Original access: read only</p>
      <IndexStatusPanel
        key={`${session.cacheNamespace}:${root.id}:${root.authorizationEpoch}`}
        rootId={root.id}
        rootEpoch={root.authorizationEpoch}
        canRequestScan={root.permissions.includes("root_admin")}
        onStatus={handleStatus}
      />
      <div className="files-toolbar">
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
        <FilterChips
          value={filters}
          onChange={(next) => {
            applyFilters(next);
            filtersButtonRef.current?.focus({preventScroll: true});
          }}
        />
      </div>
      <p className="visually-hidden" role="status" aria-live="polite" {...announcementTestId}>
        {announcement}
      </p>
      {directoryQuery.isPending ? <p role="status">Loading files…</p> : null}
      {directoryQuery.isError ? (
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
          entries={entries}
          onOpen={(entry) => {
            if (entry.kind === "directory") {
              remember();
              setSelection(null);
              navigate(`/files/${root.id}/directories/${entry.id}`);
            } else {
              detailsReturnRef.current = document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
              setSelection({namespace: session.cacheNamespace, routeKey, id: entry.id});
            }
          }}
          onLoadNext={() => directoryQuery.fetchNextPage()}
          onLoadPrevious={() => directoryQuery.fetchPreviousPage()}
          hasNext={Boolean(directoryQuery.hasNextPage)}
          hasPrevious={Boolean(directoryQuery.hasPreviousPage)}
          initialAnchor={restored ? {
            id: restored.visibleAnchorId,
            offset: restored.visibleAnchorOffset,
          } : undefined}
          onAnchorChange={(anchor) => {
            anchorRef.current = anchor;
            remember(anchor);
          }}
        />
      ) : null}
      {selectedFileId ? (
        <EntryDetailsPanel
          key={selectedFileId}
          entryId={selectedFileId}
          onClose={() => {
            const target = detailsReturnRef.current;
            setSelection(null);
            if (target?.isConnected) target.focus({preventScroll: true});
          }}
        />
      ) : null}
      <FilterPanel
        value={filters}
        open={filtersOpen}
        onApply={applyFilters}
        onCancel={() => setFiltersOpen(false)}
      />
    </section>
  );
}
