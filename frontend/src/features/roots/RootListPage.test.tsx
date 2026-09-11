import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {describe, expect, it} from "vitest";
import {AuthSessionContext} from "../auth/session";
import type {SessionResponse} from "../auth/types";
import {server} from "../../test/server";
import {RootListPage} from "./RootListPage";
import "../../styles/global.css";

const SESSION: SessionResponse = {
  user: {id: "1797b1eb-c642-4f66-99a4-a878347b4e49", username: "alice"},
  cacheNamespace: "roots-namespace".repeat(3),
};

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderRootList() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={{session: SESSION}}>
        <MemoryRouter initialEntries={["/roots"]}>
          <LocationProbe />
          <Routes>
            <Route path="/login" element={<h1>Sign in</h1>} />
            <Route path="/roots" element={<RootListPage />} />
          </Routes>
        </MemoryRouter>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
  return queryClient;
}

describe("RootListPage", () => {
  it("renders only permission-bound API roots without a host or container path", async () => {
    server.use(
      http.get("/api/v1/roots", () =>
        HttpResponse.json({
          roots: [
            {
              id: "e93b2ff7-584a-4b62-89d7-12a9f1041815",
              displayName: "Family photos",
              mode: "read_only",
              permissions: ["browse", "preview"],
              authorizationEpoch: 4,
            },
          ],
        }),
      ),
    );

    renderRootList();

    expect(await screen.findByRole("heading", {name: "Family photos"})).toBeVisible();
    expect(screen.getByText("Original access: read only")).toBeVisible();
    expect(screen.queryByText(/srv\/aegis/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/container path/i)).not.toBeInTheDocument();
  });

  it("shows a useful empty state when the account has no roots", async () => {
    server.use(
      http.get("/api/v1/roots", () => HttpResponse.json({roots: []})),
    );
    renderRootList();

    expect(await screen.findByRole("heading", {name: "No roots assigned"})).toBeVisible();
    expect(screen.getByText(/administrator can grant access/i)).toBeVisible();
  });

  it("purges private query data and routes to login on API session expiry", async () => {
    server.use(
      http.get("/api/v1/roots", () =>
        HttpResponse.json(
          {type: "authentication_required", title: "Authentication required"},
          {status: 401},
        ),
      ),
    );
    const queryClient = renderRootList();
    queryClient.setQueryData(["private", "stale-root"], "secret");

    expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/login");
    expect(queryClient.getQueryData(["private", "stale-root"])).toBeUndefined();
  });

  it("opens the Phase 2 explanation with keyboard-native activation", async () => {
    const longName = `Archive ${"very-long-name-".repeat(7)}`;
    server.use(
      http.get("/api/v1/roots", () =>
        HttpResponse.json({
          roots: [
            {
              id: "2503ab90-d801-466c-a6aa-3599fb0fe68f",
              displayName: longName,
              mode: "read_write",
              permissions: ["browse"],
              authorizationEpoch: 8,
            },
          ],
        }),
      ),
    );
    renderRootList();
    const rootButton = await screen.findByRole("button", {name: new RegExp(longName)});

    rootButton.focus();
    fireEvent.keyDown(rootButton, {key: "Enter"});
    fireEvent.click(rootButton);

    expect(screen.getByText("File browsing arrives in Phase 2.")).toBeVisible();
    expect(screen.getByText("Host capability: managed writes declared")).toBeVisible();
    expect(getComputedStyle(rootButton).minBlockSize).toBe("var(--touch-target)");
  });

  it("shows a generic bounded failure and never renders malformed names", async () => {
    server.use(
      http.get("/api/v1/roots", () =>
        HttpResponse.json({
          roots: [
            {
              id: "not-a-uuid",
              displayName: "private malformed name",
              mode: "read_only",
              permissions: ["browse"],
              authorizationEpoch: 0,
            },
          ],
        }),
      ),
    );
    renderRootList();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Roots could not be loaded. Please try again.",
      ),
    );
    expect(screen.queryByText("private malformed name")).not.toBeInTheDocument();
  });
});
