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
import {
  createFileNavigation,
  type FileNavigation,
  type FileNavigationRecord,
} from "./navigation";
import {directoryQueryOptions} from "./queries";
import type {BrowseInput, EntrySummary} from "./types";
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
  if (state === "degraded" || state === "unavailable") return "Some files could not be indexed.";
  if (state === "not_indexed" || state === "queued" || state === "scanning") {
    return "Indexing this location…";
  }
  return null;
}

function InlineFileSummary({entry}: {entry: EntrySummary}) {
  return (
    <section className="file-selection" aria-label="Selected file">
      <p className="eyebrow">Selected file</p>
      <h2 dir="auto">{entry.displayName}</h2>
      <dl>
        <div><dt>Type</dt><dd>{entry.typeHint ?? entry.kind}</dd></div>
        <div><dt>Size</dt><dd>{entry.size === null ? "Unknown" : `${entry.size} bytes`}</dd></div>
        <div><dt>Availability</dt><dd>{entry.sourceState}</dd></div>
      </dl>
      <p>Originals remain read only. Preview and download are not available here.</p>
    </section>
  );
}

export function FilesPage() {
  const {rootId = "", parentId} = useParams();
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const navigationRoute = useFileNavigationRoute();
  const navigation = navigationRoute.navigation;
  const [sessionExpiring, setSessionExpiring] = useState(false);
  const [selectedFile, setSelectedFile] = useState<EntrySummary | null>(null);
  const anchorRef = useRef<VisibleAnchor>({id: null, offset: 0});
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

  const remember = useCallback((anchor = anchorRef.current) => {
    if (!navigation) return;
    const firstPageParam = directoryQuery.data?.pageParams[0];
    navigation.remember(routeKey, {
      rootId,
      parentId: parentId ?? null,
      filters,
      sort,
      order,
      cursor: typeof firstPageParam === "string" ? firstPageParam : null,
      visibleAnchorId: anchor.id,
      visibleAnchorOffset: anchor.offset,
    });
  }, [directoryQuery.data?.pageParams, filters, navigation, order, parentId, rootId, routeKey, sort]);

  useEffect(() => {
    setSelectedFile(null);
  }, [parentId, rootId]);

  useEffect(() => () => remember(), [remember]);

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

  return (
    <section className="files-page">
      <nav className="files-page__breadcrumbs" aria-label="File location">
        <Link className="interactive" to="/roots">Roots</Link>
        {parentId ? <Link className="interactive" to={`/files/${root.id}`}>{root.displayName}</Link> : null}
      </nav>
      <p className="eyebrow">Read-only library</p>
      <h1 dir="auto">{root.displayName}</h1>
      <p className="files-page__access">Original access: read only</p>
      {directoryQuery.isPending ? <p role="status">Loading files…</p> : null}
      {directoryQuery.isError ? (
        <p className="notice notice--error" role="alert">Files could not be loaded. Please try again.</p>
      ) : null}
      {statusMessage(status?.state ?? "") ? (
        <p className={status?.state === "degraded" ? "notice notice--warning" : "notice"} role="status">
          {statusMessage(status?.state ?? "")}
        </p>
      ) : null}
      {!directoryQuery.isPending && !directoryQuery.isError && entries.length === 0 && status?.state === "ready" ? (
        <p className="files-page__empty">No files in this location.</p>
      ) : null}
      {entries.length > 0 ? (
        <VirtualFileList
          entries={entries}
          onOpen={(entry) => {
            if (entry.kind === "directory") {
              remember();
              navigate(`/files/${root.id}/directories/${entry.id}`);
            } else {
              setSelectedFile(entry);
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
      {selectedFile ? <InlineFileSummary entry={selectedFile} /> : null}
    </section>
  );
}
