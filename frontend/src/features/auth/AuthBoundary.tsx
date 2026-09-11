import {useQuery, useQueryClient} from "@tanstack/react-query";
import {
  useEffect,
  useState,
  type PropsWithChildren,
} from "react";
import {Navigate} from "react-router";
import {fetchSession} from "./api";
import {activateCacheNamespace, purgePrivateBrowserState} from "./cache";
import {AuthSessionContext, SESSION_QUERY_KEY} from "./session";

function PrivateContentSkeleton() {
  return (
    <div className="private-skeleton" role="status" aria-label="Checking session">
      <span>Checking session…</span>
    </div>
  );
}

export function AuthBoundary({children}: PropsWithChildren) {
  const queryClient = useQueryClient();
  const sessionQuery = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    retry: false,
    staleTime: 0,
  });
  const [readyNamespace, setReadyNamespace] = useState<string | null>(null);
  const [anonymous, setAnonymous] = useState(false);
  const [restoredPageChecking, setRestoredPageChecking] = useState(false);

  useEffect(() => {
    const session = sessionQuery.data;
    if (!session || readyNamespace === session.cacheNamespace) return;
    let active = true;
    void (async () => {
      const cacheWasPurged = await activateCacheNamespace(
        queryClient,
        session.cacheNamespace,
      );
      if (!active) return;
      if (cacheWasPurged) queryClient.setQueryData(SESSION_QUERY_KEY, session);
      setAnonymous(false);
      setReadyNamespace(session.cacheNamespace);
    })();
    return () => {
      active = false;
    };
  }, [queryClient, readyNamespace, sessionQuery.data]);

  useEffect(() => {
    if (!sessionQuery.isError) return;
    let active = true;
    void purgePrivateBrowserState(queryClient).then(() => {
      if (active) setAnonymous(true);
    });
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
          await purgePrivateBrowserState(queryClient);
          setAnonymous(true);
        }
        setRestoredPageChecking(false);
      });
    };
    window.addEventListener("pageshow", revalidate);
    return () => window.removeEventListener("pageshow", revalidate);
  }, [queryClient, sessionQuery]);

  if (anonymous) return <Navigate to="/login" replace state={{reason: "session"}} />;
  if (
    sessionQuery.isPending ||
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
