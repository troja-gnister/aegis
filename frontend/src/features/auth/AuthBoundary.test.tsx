import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {afterEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {AuthBoundary} from "./AuthBoundary";
import {LogoutButton} from "./LogoutButton";
import {purgePrivateBrowserState} from "./cache";

const SESSION = {
  user: {id: "1e999c0b-8138-4dd5-80ea-e651f689eaa1", username: "alice"},
  cacheNamespace: "namespace-a".repeat(4),
};

const clients = new Set<QueryClient>();

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderBoundary({withLogout = false}: {withLogout?: boolean} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {queries: {retry: false}},
  });
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
