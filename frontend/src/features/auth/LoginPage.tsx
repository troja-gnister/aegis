import {useQueryClient} from "@tanstack/react-query";
import {useState, type FormEvent} from "react";
import {useNavigate} from "react-router";
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
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
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
        <form onSubmit={submit} noValidate>
          <label htmlFor="username">Username</label>
          <input
            id="username"
            name="username"
            type="text"
            autoComplete="username"
            required
            maxLength={150}
            disabled={submitting}
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={512}
            disabled={submitting}
          />
          {errorMessage ? <p role="alert">{errorMessage}</p> : null}
          <button type="submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
