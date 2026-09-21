import {useQueryClient} from "@tanstack/react-query";
import {useEffect, useRef, useState, type FormEvent} from "react";
import {useNavigate} from "react-router";
import {ApiProblem} from "../../api/problem";
import {fetchSession, loginWithCredentials} from "./api";
import {
  activateCacheNamespace,
  PrivateStateCleanupError,
  purgePrivateBrowserState,
} from "./cache";
import {
  beginSignIn,
  completeSignOut,
  isSessionTransitionCurrent,
  openSessionAfterLogin,
  SESSION_QUERY_KEY,
  useSessionAccess,
} from "./session";

function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiProblem && error.status === 429) {
    const delay = error.retryAfterSeconds;
    if (delay !== undefined) {
      return `Please wait ${delay} ${delay === 1 ? "second" : "seconds"} before trying again.`;
    }
  }
  if (error instanceof ApiProblem && error.status === 401) {
    return "The username or password was not accepted.";
  }
  return "Sign in could not be completed. Please try again.";
}

export function LoginPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const sessionAccess = useSessionAccess();
  const logoutPending = sessionAccess === "signing_out";
  const cleanupFailed = sessionAccess === "cleanup_failed";
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const mounted = useRef(false);
  const attempt = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      attempt.current += 1;
    };
  }, []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting || logoutPending || cleanupFailed) return;
    const currentAttempt = ++attempt.current;
    const generation = beginSignIn();
    // A response may outlive this form or a newer sign-in/sign-out transition.
    const isCurrent = () => mounted.current && attempt.current === currentAttempt &&
      isSessionTransitionCurrent(generation);
    setSubmitting(true);
    setErrorMessage(null);
    const data = new FormData(event.currentTarget);
    const username = String(data.get("username") ?? "");
    const password = String(data.get("password") ?? "");
    try {
      await loginWithCredentials(username, password);
      if (!isCurrent()) return;
      await purgePrivateBrowserState(queryClient);
      if (!isCurrent()) return;
      const session = await fetchSession();
      if (!isCurrent()) return;
      await activateCacheNamespace(queryClient, session.cacheNamespace, isCurrent);
      if (!isCurrent()) return;
      queryClient.setQueryData(SESSION_QUERY_KEY, session);
      openSessionAfterLogin();
      navigate("/roots", {replace: true});
    } catch (error) {
      if (error instanceof PrivateStateCleanupError) {
        if (isSessionTransitionCurrent(generation)) {
          completeSignOut(generation, false, false);
        }
      } else if (isCurrent()) {
        setErrorMessage(loginErrorMessage(error));
      }
    } finally {
      if (mounted.current && attempt.current === currentAttempt) setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <p className="login-card__brand">Aegis</p>
        <h1 id="login-title">Sign in</h1>
        <p>Access the roots assigned to your account.</p>
        {logoutPending ? <p role="status">Signing out…</p> : null}
        {sessionAccess === "unconfirmed" ? (
          <p role="alert">Local data was cleared. Server sign-out could not be confirmed.</p>
        ) : null}
        {cleanupFailed ? (
          <p role="alert">
            Private browser data could not be fully cleared. Close this tab before signing in again.
          </p>
        ) : null}
        <form onSubmit={submit} noValidate>
          <label htmlFor="username">Username</label>
          <input
            id="username"
            name="username"
            type="text"
            autoComplete="username"
            required
            maxLength={150}
            disabled={submitting || logoutPending || cleanupFailed}
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={512}
            disabled={submitting || logoutPending || cleanupFailed}
          />
          {errorMessage ? <p role="alert">{errorMessage}</p> : null}
          <button type="submit" disabled={submitting || logoutPending || cleanupFailed}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
