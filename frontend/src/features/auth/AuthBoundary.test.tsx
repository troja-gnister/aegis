import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {afterEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {AuthBoundary} from "./AuthBoundary";
import {LogoutButton} from "./LogoutButton";
import {purgePrivateBrowserState} from "./cache";
import {SESSION_QUERY_KEY} from "./session";

const SESSION = {
  user: {id: "1e999c0b-8138-4dd5-80ea-e651f689eaa1", username: "alice"},
  cacheNamespace: "namespace-a".repeat(4),
};

const clients = new Set<QueryClient>();

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderBoundary({withLogout = false, cachedSession = false}: {
  withLogout?: boolean;
  cachedSession?: boolean;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {queries: {retry: false}},
  });
  if (cachedSession) queryClient.setQueryData(SESSION_QUERY_KEY, SESSION);
  clients.add(queryClient);
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/roots"]}>
        <LocationProbe />
        <Routes>
          <Route path="/login" element={<h1>Sign in</h1>} />
          <Route
            path="/roots"
            element={
              <AuthBoundary>
                <h1>Private family archive</h1>
                {withLogout ? <LogoutButton /> : null}
              </AuthBoundary>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

function persistedPageShow(): Event {
  const event = new Event("pageshow");
  Object.defineProperty(event, "persisted", {value: true});
  return event;
}

afterEach(async () => {
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});

describe("AuthBoundary", () => {
  it("never trusts a cached session while mount revalidation is still pending", async () => {
    let release = () => {};
    let requested = false;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get("/api/v1/auth/session", async () => {
      requested = true;
      await gate;
      return HttpResponse.json({type: "authentication_required"}, {status: 401});
    }));
    renderBoundary({cachedSession: true});
    try {
      await waitFor(() => expect(requested).toBe(true));
      expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Checking session")).toBeVisible();
    } finally {
      release();
    }
    expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
  });

  it("clears local private state when logout returns an error after revocation", async () => {
    let revoked = false;
    server.use(
      http.get("/api/v1/auth/session", () => revoked
        ? HttpResponse.json({type: "authentication_required"}, {status: 401})
        : HttpResponse.json(SESSION)),
      http.post("/api/v1/auth/logout", () => {
        revoked = true;
        return new HttpResponse(null, {status: 500});
      }),
    );
    const client = renderBoundary({withLogout: true});
    await screen.findByText("Private family archive");
    client.setQueryData(["private", "root"], "secret");
    fireEvent.click(screen.getByRole("button", {name: "Sign out"}));
    await waitFor(() => expect(client.getQueryData(["private", "root"])).toBeUndefined());
    expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
    expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
  });

  it("closes private content and clears caches before a delayed logout response", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
      http.post("/api/v1/auth/logout", async () => {
        await gate;
        return HttpResponse.error();
      }),
    );
    const client = renderBoundary({withLogout: true});
    await screen.findByText("Private family archive");
    client.setQueryData(["private", "root"], "secret");
    fireEvent.click(screen.getByRole("button", {name: "Sign out"}));
    try {
      expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
      expect(client.getQueryData(["private", "root"])).toBeUndefined();
      expect(screen.getByRole("heading", {name: "Sign in"})).toBeVisible();
    } finally {
      release();
    }
  });

  it("never flashes private content while the initial session is checking", async () => {
    let releaseSession: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      releaseSession = resolve;
    });
    server.use(
      http.get("/api/v1/auth/session", async () => {
        await gate;
        return HttpResponse.json(SESSION);
      }),
    );

    renderBoundary();

    expect(screen.getByLabelText("Checking session")).toBeInTheDocument();
    expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
    releaseSession();
    expect(await screen.findByText("Private family archive")).toBeVisible();
  });

  it("purges state and replaces private content with login on 401", async () => {
    const queryClient = renderBoundary();
    queryClient.setQueryData(["private", "root"], "old private value");

    expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
    expect(queryClient.getQueryData(["private", "root"])).toBeUndefined();
    expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
  });

  it("hides restored-page content until bfcache session revalidation succeeds", async () => {
    let calls = 0;
    let releaseRevalidation: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      releaseRevalidation = resolve;
    });
    server.use(
      http.get("/api/v1/auth/session", async () => {
        calls += 1;
        if (calls > 1) await gate;
        return HttpResponse.json(SESSION);
      }),
    );
    renderBoundary();
    expect(await screen.findByText("Private family archive")).toBeVisible();

    act(() => window.dispatchEvent(persistedPageShow()));

    expect(screen.getByLabelText("Checking session")).toBeInTheDocument();
    expect(screen.queryByText("Private family archive")).not.toBeInTheDocument();
    releaseRevalidation();
    expect(await screen.findByText("Private family archive")).toBeVisible();
    expect(calls).toBe(2);
  });

  it("purges the prior query cache before rendering a changed namespace", async () => {
    let namespace = SESSION.cacheNamespace;
    server.use(
      http.get("/api/v1/auth/session", () =>
        HttpResponse.json({...SESSION, cacheNamespace: namespace}),
      ),
    );
    const queryClient = renderBoundary();
    expect(await screen.findByText("Private family archive")).toBeVisible();
    queryClient.setQueryData(["private", "old-account"], "secret");
    namespace = "namespace-b".repeat(4);

    act(() => window.dispatchEvent(persistedPageShow()));

    await waitFor(() =>
      expect(queryClient.getQueryData(["private", "old-account"])).toBeUndefined(),
    );
    expect(await screen.findByText("Private family archive")).toBeVisible();
  });

  it("logs out with CSRF, purges cached state, and replaces history", async () => {
    let logoutCsrf = "";
    server.use(
      http.get("/api/v1/auth/session", () => HttpResponse.json(SESSION)),
      http.post("/api/v1/auth/logout", ({request}) => {
        logoutCsrf = request.headers.get("X-CSRFToken") ?? "";
        return new HttpResponse(null, {status: 204});
      }),
    );
    const queryClient = renderBoundary({withLogout: true});
    queryClient.setQueryData(["private", "root"], "secret");
    await screen.findByText("Private family archive");

    fireEvent.click(screen.getByRole("button", {name: "Sign out"}));

    expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
    expect(queryClient.getQueryData(["private", "root"])).toBeUndefined();
    expect(logoutCsrf).toBe("csrf-test-token");
  });
});
