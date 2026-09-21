import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation, useNavigate} from "react-router";
import {afterEach, beforeEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {clearCsrfToken} from "./api";
import {LoginPage} from "./LoginPage";
import {AuthBoundary} from "./AuthBoundary";
import {LogoutButton} from "./LogoutButton";
import {purgePrivateBrowserState, registerPrivateStateCleanup} from "./cache";
import {SESSION_QUERY_KEY, useAuthSession} from "./session";

const SESSION = {
  user: {id: "6b824aeb-a9b7-4685-b359-cf22a437076d", username: "alice"},
  cacheNamespace: "login-namespace".repeat(3),
};

beforeEach(() => clearCsrfToken());
const clients = new Set<QueryClient>();
afterEach(async () => {
  await Promise.all([...clients].map(purgePrivateBrowserState));
  clients.clear();
});

function LocationProbe() {
  const navigate = useNavigate();
  return <>
    <output aria-label="Current route">{useLocation().pathname}</output>
    <button onClick={() => navigate(-1)}>Back</button>
  </>;
}

function renderLogin() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/login"]}>
        <LocationProbe />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/roots" element={<h1>Authorized roots</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

function PrivateSession() {
  const {session} = useAuthSession();
  return <>
    <h1>Private archive for {session.user.username}</h1>
    <LogoutButton />
  </>;
}

function renderLoginWithHistory({withConcurrentLogout = false} = {}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(client);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/roots", "/login"]}>
        <LocationProbe />
        <Routes>
          <Route path="/login" element={<><LoginPage />{withConcurrentLogout ? <LogoutButton /> : null}</>} />
          <Route path="/roots" element={<AuthBoundary><PrivateSession /></AuthBoundary>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

function networkGate(path: string) {
  let release = () => {};
  let requested = () => {};
  let delivered = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const requestStarted = new Promise<void>((resolve) => { requested = resolve; });
  const responseDelivered = new Promise<void>((resolve) => { delivered = resolve; });
  let requestId: string | undefined;
  const listener = (event: {requestId: string}) => {
    if (event.requestId === requestId) delivered();
  };
  server.events.on("response:mocked", listener);
  return {
    requestStarted,
    async hold(id: string) {
      requestId = id;
      requested();
      await pending;
    },
    async deliver() {
      await act(async () => {
        release();
        await responseDelivered;
        // Drain fetch/body continuations after MSW has delivered this response.
        await new Promise<void>((resolve) => setTimeout(resolve, 0));
      });
      server.events.removeListener("response:mocked", listener);
    },
    release() {
      release();
      server.events.removeListener("response:mocked", listener);
    },
    path,
  };
}

function submitCredentials() {
  fireEvent.change(screen.getByLabelText("Username"), {target: {value: "alice"}});
  fireEvent.change(screen.getByLabelText("Password"), {
    target: {value: "correct horse battery staple"},
  });
  fireEvent.click(screen.getByRole("button", {name: "Sign in"}));
}

describe("LoginPage", () => {
  it("blocks sign-in with a truthful safe message when private cleanup fails", async () => {
    server.use(
      http.post("/api/v1/auth/login", () => HttpResponse.json({user: SESSION.user})),
    );
    const unregister = registerPrivateStateCleanup(() => {
      throw new Error("private cleanup source path");
    });
    try {
      renderLogin();
      submitCredentials();

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Private browser data could not be fully cleared. Close this tab before signing in again.",
      );
      expect(screen.getByRole("button", {name: "Sign in"})).toBeDisabled();
      expect(screen.queryByText("private cleanup source path")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
    } finally {
      unregister();
    }
  });

  it.each(["login", "session"] as const)(
    "does not commit an unmounted %s response even without a newer auth transition",
    async (stage) => {
      const stale = networkGate(`/api/v1/auth/${stage}`);
      const liveSession = {...SESSION, user: {...SESSION.user, username: "bob"}, cacheNamespace: "live-account"};
      let sessionCalls = 0;
      server.use(
        http.post("/api/v1/auth/login", async ({requestId}) => {
          if (stage === "login") await stale.hold(requestId);
          return HttpResponse.json({user: SESSION.user});
        }),
        http.get("/api/v1/auth/session", async ({requestId}) => {
          sessionCalls += 1;
          if (stage === "session" && sessionCalls === 1) {
            await stale.hold(requestId);
            return HttpResponse.json(SESSION);
          }
          return HttpResponse.json(liveSession);
        }),
      );
      const client = renderLoginWithHistory();
      try {
        submitCredentials();
        await stale.requestStarted;
        fireEvent.click(screen.getByRole("button", {name: "Back"}));
        await screen.findByRole("heading", {name: "Private archive for bob"});
        client.setQueryData(["private", "bob"], "fresh selection");
        await stale.deliver();
        expect(client.getQueryData(["private", "bob"])).toBe("fresh selection");
        expect(client.getQueryData(SESSION_QUERY_KEY)).toEqual(liveSession);
        expect(screen.getByRole("heading", {name: "Private archive for bob"})).toBeVisible();
      } finally {
        stale.release();
      }
    },
  );

  it("ignores a pending login when a newer logout begins without unmounting the form", async () => {
    const login = networkGate("/api/v1/auth/login");
    const logout = networkGate("/api/v1/auth/logout");
    server.use(
      http.post(login.path, async ({requestId}) => {
        await login.hold(requestId);
        return HttpResponse.json({user: SESSION.user});
      }),
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
      http.post(logout.path, async ({requestId}) => {
        await logout.hold(requestId);
        return new HttpResponse(null, {status: 500});
      }),
    );
    const client = renderLoginWithHistory({withConcurrentLogout: true});
    try {
      submitCredentials();
      await login.requestStarted;
      fireEvent.click(screen.getByRole("button", {name: "Sign out"}));
      await logout.requestStarted;
      await login.deliver();
      expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
      expect(client.getQueryData(SESSION_QUERY_KEY)).toBeUndefined();
      expect(screen.getByLabelText("Username")).toBeDisabled();
    } finally {
      login.release();
      await logout.deliver();
      logout.release();
    }
  });

  it.each(["pending", "completed"] as const)(
    "ignores an unmounted login session response when a newer logout is %s",
    async (logoutState) => {
      const staleSession = networkGate("/api/v1/auth/session");
      const logout = networkGate("/api/v1/auth/logout");
      let sessionCalls = 0;
      server.use(
        http.post("/api/v1/auth/login", () => HttpResponse.json({user: SESSION.user})),
        http.get(staleSession.path, async ({requestId}) => {
          sessionCalls += 1;
          if (sessionCalls === 1) await staleSession.hold(requestId);
          return HttpResponse.json(SESSION);
        }),
        http.post(logout.path, async ({requestId}) => {
          await logout.hold(requestId);
          // An uncertain logout must remain closed even if the server session lives.
          return new HttpResponse(null, {status: 500});
        }),
      );
      const client = renderLoginWithHistory();
      try {
        submitCredentials();
        await staleSession.requestStarted;
        fireEvent.click(screen.getByRole("button", {name: "Back"}));
        await screen.findByRole("heading", {name: "Private archive for alice"});
        fireEvent.click(screen.getByRole("button", {name: "Sign out"}));
        await logout.requestStarted;
        if (logoutState === "completed") await logout.deliver();
        await staleSession.deliver();

        expect(screen.queryByRole("heading", {name: "Private archive for alice"})).not.toBeInTheDocument();
        expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
        expect(client.getQueryData(SESSION_QUERY_KEY)).toBeUndefined();
        if (logoutState === "pending") expect(screen.getByRole("button", {name: "Sign in"})).toBeDisabled();
      } finally {
        staleSession.release();
        if (logoutState === "pending") await logout.deliver();
        logout.release();
      }
    },
  );

  it("does not overwrite the private cache of a newer validated login", async () => {
    const staleSession = networkGate("/api/v1/auth/session");
    const newerSession = {
      user: {id: "9d8d0de4-fc41-4881-ab1b-dcdd53a29a86", username: "bob"},
      cacheNamespace: "newer-login-namespace".repeat(3),
    };
    let sessionCalls = 0;
    let currentSession = SESSION;
    server.use(
      http.post("/api/v1/auth/login", async ({request}) => {
        const credentials = await request.json() as {username: string};
        if (credentials.username === "bob") currentSession = newerSession;
        return HttpResponse.json({user: currentSession.user});
      }),
      http.get(staleSession.path, async ({requestId}) => {
        sessionCalls += 1;
        if (sessionCalls === 1) {
          await staleSession.hold(requestId);
          return HttpResponse.json(SESSION);
        }
        return HttpResponse.json(currentSession);
      }),
      http.post("/api/v1/auth/logout", () => new HttpResponse(null, {status: 500})),
    );
    const client = renderLoginWithHistory();
    try {
      submitCredentials();
      await staleSession.requestStarted;
      fireEvent.click(screen.getByRole("button", {name: "Back"}));
      await screen.findByRole("heading", {name: "Private archive for alice"});
      fireEvent.click(screen.getByRole("button", {name: "Sign out"}));
      await waitFor(() => expect(screen.getByRole("button", {name: "Sign in"})).toBeEnabled());
      fireEvent.change(screen.getByLabelText("Username"), {target: {value: "bob"}});
      fireEvent.change(screen.getByLabelText("Password"), {target: {value: "new password"}});
      fireEvent.click(screen.getByRole("button", {name: "Sign in"}));
      await screen.findByRole("heading", {name: "Private archive for bob"});
      client.setQueryData(["private", "bob"], "new private selection");
      await staleSession.deliver();

      expect(screen.getByRole("heading", {name: "Private archive for bob"})).toBeVisible();
      expect(client.getQueryData(SESSION_QUERY_KEY)).toEqual(newerSession);
      expect(client.getQueryData(["private", "bob"])).toBe("new private selection");
      expect(screen.queryByRole("heading", {name: "Private archive for alice"})).not.toBeInTheDocument();
    } finally {
      staleSession.release();
    }
  });

  it("uses password-manager-compatible fields and replaces the login route", async () => {
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({user: SESSION.user}),
      ),
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
    );
    renderLogin();

    expect(screen.getByLabelText("Username")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    submitCredentials();

    expect(await screen.findByRole("heading", {name: "Authorized roots"})).toBeVisible();
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/roots");
  });

  it("shows one generic message for invalid credentials", async () => {
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json(
          {type: "invalid_credentials", title: "private server title"},
          {status: 401},
        ),
      ),
    );
    renderLogin();
    submitCredentials();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The username or password was not accepted.",
    );
    expect(screen.queryByText("private server title")).not.toBeInTheDocument();
  });

  it("shows only a bounded retry delay when login is throttled", async () => {
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json(
          {type: "login_throttled", title: "Unable to sign in"},
          {status: 429, headers: {"Retry-After": "27"}},
        ),
      ),
    );
    renderLogin();
    submitCredentials();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Please wait 27 seconds before trying again.",
    );
  });

  it("refreshes CSRF and retries exactly once for an explicit stale-CSRF response", async () => {
    let csrfCalls = 0;
    let loginCalls = 0;
    server.use(
      http.get("/api/v1/auth/csrf", () => {
        csrfCalls += 1;
        return HttpResponse.json({csrfToken: `csrf-${csrfCalls}`});
      }),
      http.post("/api/v1/auth/login", ({request}) => {
        loginCalls += 1;
        if (loginCalls === 1) {
          expect(request.headers.get("X-CSRFToken")).toBe("csrf-1");
          return HttpResponse.json(
            {type: "csrf_failed", title: "Request verification failed"},
            {status: 403},
          );
        }
        expect(request.headers.get("X-CSRFToken")).toBe("csrf-2");
        return HttpResponse.json({user: SESSION.user});
      }),
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
    );
    renderLogin();
    submitCredentials();

    await screen.findByRole("heading", {name: "Authorized roots"});
    expect(csrfCalls).toBe(2);
    expect(loginCalls).toBe(2);
  });

  it("disables duplicate submission while authentication is pending", async () => {
    let loginCalls = 0;
    let releaseLogin: () => void = () => undefined;
    const pendingLogin = new Promise<void>((resolve) => {
      releaseLogin = resolve;
    });
    server.use(
      http.post("/api/v1/auth/login", async () => {
        loginCalls += 1;
        await pendingLogin;
        return HttpResponse.json({user: SESSION.user});
      }),
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
    );
    renderLogin();
    submitCredentials();
    const submit = screen.getByRole("button", {name: "Signing in…"});
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    await waitFor(() => expect(loginCalls).toBe(1));
    releaseLogin();
    await waitFor(() => expect(screen.getByLabelText("Current route")).toHaveTextContent("/roots"));
  });
});
