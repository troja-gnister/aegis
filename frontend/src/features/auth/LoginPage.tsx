import {useQueryClient} from "@tanstack/react-query";
import {useState, type FormEvent} from "react";
import {useLocation, useNavigate} from "react-router";
import {ApiProblem} from "../../api/problem";
import {fetchSession, loginWithCredentials} from "./api";
import {activateCacheNamespace, purgePrivateBrowserState} from "./cache";
import {SESSION_QUERY_KEY} from "./session";

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
  const location = useLocation();
  const logoutPending = location.state?.logoutPending === true;
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting || logoutPending) return;
    setSubmitting(true);
    setErrorMessage(null);
    const data = new FormData(event.currentTarget);
    const username = String(data.get("username") ?? "");
    const password = String(data.get("password") ?? "");
    try {
      await loginWithCredentials(username, password);
      await purgePrivateBrowserState(queryClient);
      const session = await fetchSession();
      await activateCacheNamespace(queryClient, session.cacheNamespace);
      queryClient.setQueryData(SESSION_QUERY_KEY, session);
      navigate("/roots", {replace: true});
    } catch (error) {
      setErrorMessage(loginErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <p className="login-card__brand">Aegis</p>
        <h1 id="login-title">Sign in</h1>
        <p>Access the roots assigned to your account.</p>
        {logoutPending ? <p role="status">Signing out…</p> : null}
        {location.state?.logoutUnconfirmed ? (
          <p role="alert">Local data was cleared. Server sign-out could not be confirmed.</p>
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
            disabled={submitting || logoutPending}
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={512}
            disabled={submitting || logoutPending}
          />
          {errorMessage ? <p role="alert">{errorMessage}</p> : null}
          <button type="submit" disabled={submitting || logoutPending}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
