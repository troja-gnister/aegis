import {useQuery, useQueryClient} from "@tanstack/react-query";
import {useEffect, useState} from "react";
import {useNavigate} from "react-router";
import {ApiProblem} from "../../api/problem";
import {purgePrivateBrowserState} from "../auth/cache";
import {beginSignOut, completeSignOut, useAuthSession} from "../auth/session";
import {fetchRoots} from "./api";

/**
 * The current session's root list and the browsable root `rootId`. A 401
 * from the root list closes private access, purges private state and goes to
 * sign-in; `sessionExpiring` is true from then on.
 */
export function useAuthorizedRoot(rootId: string, enabled = true) {
  const {session} = useAuthSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [sessionExpiring, setSessionExpiring] = useState(false);
  const rootQuery = useQuery({
    queryKey: ["roots", session.cacheNamespace],
    queryFn: fetchRoots,
    retry: false,
    enabled,
  });
  const root = rootQuery.data?.roots.find((candidate) =>
    candidate.id === rootId && candidate.permissions.includes("browse"),
  );

  useEffect(() => {
    if (!(rootQuery.error instanceof ApiProblem) || rootQuery.error.status !== 401 || sessionExpiring) return;
    setSessionExpiring(true);
    const generation = beginSignOut();
    void purgePrivateBrowserState(queryClient).then(
      () => completeSignOut(generation, true),
      () => completeSignOut(generation, true, false),
    ).then(() => navigate("/login", {replace: true, state: {reason: "session"}}));
  }, [navigate, queryClient, rootQuery.error, sessionExpiring]);

  return {rootQuery, root, sessionExpiring};
}
