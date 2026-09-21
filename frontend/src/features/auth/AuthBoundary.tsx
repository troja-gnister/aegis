import {useQuery, useQueryClient} from "@tanstack/react-query";
import {
  useEffect,
  useState,
  type PropsWithChildren,
} from "react";
import {Navigate} from "react-router";
import {ApiProblem} from "../../api/problem";
import {fetchSession} from "./api";
import {activateCacheNamespace, purgePrivateBrowserState} from "./cache";
import {
  AuthSessionContext,
  beginSignOut,
  captureSessionTransition,
  completeSignOut,
  isSessionAccessOpen,
  isSessionTransitionCurrent,
  SESSION_QUERY_KEY,
  useSessionAccess,
} from "./session";

function PrivateContentSkeleton() {
  return (
    <div className="private-skeleton" role="status" aria-label="Checking session">
      <span>Checking session…</span>
    </div>
  );
}

export function AuthBoundary({children}: PropsWithChildren) {
  const queryClient = useQueryClient();
  const sessionAccess = useSessionAccess();
  const sessionQuery = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    retry: false,
    staleTime: 0,
    refetchOnMount: "always",
    // Pending observer effects must consult current authority after a logout purge.
    enabled: isSessionAccessOpen,
  });
  const [readyNamespace, setReadyNamespace] = useState<string | null>(null);
  const [anonymous, setAnonymous] = useState(false);
  const [restoredPageChecking, setRestoredPageChecking] = useState(false);

  useEffect(() => {
    const session = sessionQuery.data;
    if (!session || !sessionQuery.isFetchedAfterMount || sessionQuery.isFetching ||
        readyNamespace === session.cacheNamespace) return;
    let active = true;
    const generation = captureSessionTransition();
    void (async () => {
      try {
        const cacheWasPurged = await activateCacheNamespace(
          queryClient,
          session.cacheNamespace,
          () => active && isSessionTransitionCurrent(generation),
        );
        if (!active) return;
        if (cacheWasPurged) queryClient.setQueryData(SESSION_QUERY_KEY, session);
        setAnonymous(false);
        setReadyNamespace(session.cacheNamespace);
      } catch {
        completeSignOut(generation, false, false);
        if (active) setAnonymous(true);
      }
    })();
    return () => {
      active = false;
    };
  }, [queryClient, readyNamespace, sessionQuery.data, sessionQuery.isFetchedAfterMount, sessionQuery.isFetching]);

  useEffect(() => {
    if (!sessionQuery.isError) return;
    const generation = beginSignOut();
    const confirmed = sessionQuery.error instanceof ApiProblem && sessionQuery.error.status === 401;
    let active = true;
    void purgePrivateBrowserState(queryClient).then(
      () => {
        completeSignOut(generation, confirmed);
        if (active) setAnonymous(true);
      },
      () => {
        completeSignOut(generation, confirmed, false);
        if (active) setAnonymous(true);
      },
    );
    return () => {
      active = false;
    };
  }, [queryClient, sessionQuery.isError]);

  useEffect(() => {
    const revalidate = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      setRestoredPageChecking(true);
      void sessionQuery.refetch().then(async (result) => {
        if (result.error) {
          const generation = beginSignOut();
          const confirmed = result.error instanceof ApiProblem && result.error.status === 401;
          const cleanupSucceeded = await purgePrivateBrowserState(queryClient).then(
            () => true,
            () => false,
          );
          completeSignOut(generation, confirmed, cleanupSucceeded);
          setAnonymous(true);
        }
        setRestoredPageChecking(false);
      });
    };
    window.addEventListener("pageshow", revalidate);
    return () => window.removeEventListener("pageshow", revalidate);
  }, [queryClient, sessionQuery]);

  if (anonymous || sessionAccess !== "open") {
    return <Navigate to="/login" replace state={{reason: "session"}} />;
  }
  if (
    sessionQuery.isPending ||
    !sessionQuery.isFetchedAfterMount ||
    sessionQuery.isFetching ||
    sessionQuery.isError ||
    restoredPageChecking ||
    !sessionQuery.data ||
    readyNamespace !== sessionQuery.data.cacheNamespace
  ) {
    return <PrivateContentSkeleton />;
  }
  return (
    <AuthSessionContext.Provider value={{session: sessionQuery.data}}>
      {children}
    </AuthSessionContext.Provider>
  );
}
