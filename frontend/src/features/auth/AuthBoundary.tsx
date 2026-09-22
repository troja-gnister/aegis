import {useQuery, useQueryClient} from "@tanstack/react-query";
import {
  useEffect,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {Navigate, useLocation, useNavigationType} from "react-router";
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

export function PrivateContentGate({checking, children}: {
  checking: boolean;
  children: ReactNode;
}) {
  return checking ? <PrivateContentSkeleton /> : children;
}

type AuthBoundaryProps = {
  children: ReactNode | ((state: {checking: boolean}) => ReactNode);
};

export function AuthBoundary({children}: AuthBoundaryProps) {
  const queryClient = useQueryClient();
  const sessionAccess = useSessionAccess();
  const location = useLocation();
  const navigationType = useNavigationType();
  const previousLocationKeyRef = useRef(location.key);
  const pendingPopKeyRef = useRef<string | null>(null);
  const [, renderAfterHistoryCheck] = useReducer((value: number) => value + 1, 0);
  if (previousLocationKeyRef.current !== location.key) {
    previousLocationKeyRef.current = location.key;
    pendingPopKeyRef.current = navigationType === "POP" ? location.key : null;
  }
  const popChecking = pendingPopKeyRef.current === location.key;
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
    if (!sessionQuery.isError || pendingPopKeyRef.current === location.key) return;
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
  }, [location.key, queryClient, sessionQuery.error, sessionQuery.isError]);

  useEffect(() => {
    if (!popChecking) return;
    const checkedLocationKey = location.key;
    const generation = captureSessionTransition();
    let active = true;
    void sessionQuery.refetch({cancelRefetch: true}).then(async (result) => {
      if (
        !active || pendingPopKeyRef.current !== checkedLocationKey ||
        !isSessionTransitionCurrent(generation) || !isSessionAccessOpen()
      ) return;
      if (result.error) {
        const confirmed = result.error instanceof ApiProblem && result.error.status === 401;
        const signOutGeneration = beginSignOut();
        const cleanupSucceeded = await purgePrivateBrowserState(queryClient).then(
          () => true,
          () => false,
        );
        if (!active || pendingPopKeyRef.current !== checkedLocationKey) return;
        completeSignOut(signOutGeneration, confirmed, cleanupSucceeded);
        setAnonymous(true);
        return;
      }
      const session = result.data;
      if (!session) return;
      try {
        const cacheWasPurged = await activateCacheNamespace(
          queryClient,
          session.cacheNamespace,
          () => active && pendingPopKeyRef.current === checkedLocationKey &&
            isSessionTransitionCurrent(generation),
        );
        if (
          !active || pendingPopKeyRef.current !== checkedLocationKey ||
          !isSessionTransitionCurrent(generation)
        ) return;
        if (cacheWasPurged) queryClient.setQueryData(SESSION_QUERY_KEY, session);
        setAnonymous(false);
        setReadyNamespace(session.cacheNamespace);
        pendingPopKeyRef.current = null;
        renderAfterHistoryCheck();
      } catch {
        completeSignOut(generation, false, false);
        if (active) setAnonymous(true);
      }
    });
    return () => {
      active = false;
      void queryClient.cancelQueries({queryKey: SESSION_QUERY_KEY, exact: true});
    };
  }, [location.key, popChecking, queryClient, sessionQuery.refetch]);

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
    sessionQuery.isError ||
    !sessionQuery.data ||
    readyNamespace !== sessionQuery.data.cacheNamespace
  ) {
    return <PrivateContentSkeleton />;
  }
  const checking = sessionQuery.isFetching || restoredPageChecking || popChecking;
  const content = typeof children === "function"
    ? children({checking})
    : <PrivateContentGate checking={checking}>{children}</PrivateContentGate>;
  return (
    <AuthSessionContext.Provider value={{session: sessionQuery.data}}>
      {content}
    </AuthSessionContext.Provider>
  );
}
