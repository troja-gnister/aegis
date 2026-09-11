import {useQuery, useQueryClient} from "@tanstack/react-query";
import {useEffect, useState} from "react";
import {useNavigate} from "react-router";
import {ApiProblem} from "../../api/problem";
import {purgePrivateBrowserState} from "../auth/cache";
import {useAuthSession} from "../auth/session";
import {fetchRoots} from "./api";
import {RootCard} from "./RootCard";

export function RootListPage() {
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [sessionExpiring, setSessionExpiring] = useState(false);
  const rootsQuery = useQuery({
    queryKey: ["roots", session.cacheNamespace],
    queryFn: fetchRoots,
    retry: false,
  });

  useEffect(() => {
    if (
      !(rootsQuery.error instanceof ApiProblem) ||
      rootsQuery.error.status !== 401 ||
      sessionExpiring
    ) {
      return;
    }
    setSessionExpiring(true);
    void purgePrivateBrowserState(queryClient).then(() => {
      navigate("/login", {replace: true, state: {reason: "session"}});
    });
  }, [navigate, queryClient, rootsQuery.error, sessionExpiring]);

  if (rootsQuery.isPending || sessionExpiring) {
    return (
      <section className="roots-page" aria-label="Loading roots">
        <div className="root-list-skeleton" role="status">
          Loading roots…
        </div>
      </section>
    );
  }
  if (rootsQuery.isError) {
    return (
      <section className="roots-page">
        <h1>Your roots</h1>
        <p className="notice notice--error" role="alert">
          Roots could not be loaded. Please try again.
        </p>
      </section>
    );
  }
  if (rootsQuery.data.roots.length === 0) {
    return (
      <section className="roots-page roots-page--empty">
        <p className="eyebrow">Library</p>
        <h1>No roots assigned</h1>
        <p>An administrator can grant access to a configured root.</p>
      </section>
    );
  }
  return (
    <section className="roots-page">
      <p className="eyebrow">Library</p>
      <h1>Your roots</h1>
      <p className="roots-page__intro">
        Browse the storage areas assigned to your account. Mounted originals are
        always read only.
      </p>
      <div className="root-grid">
        {rootsQuery.data.roots.map((root) => (
          <RootCard key={root.id} root={root} />
        ))}
      </div>
    </section>
  );
}
