import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter, Route, Routes, useLocation} from "react-router";
import {beforeEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {clearCsrfToken} from "./api";
import {LoginPage} from "./LoginPage";

const SESSION = {
  user: {id: "6b824aeb-a9b7-4685-b359-cf22a437076d", username: "alice"},
  cacheNamespace: "login-namespace".repeat(3),
};

beforeEach(() => clearCsrfToken());

function LocationProbe() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderLogin() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
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

function submitCredentials() {
  fireEvent.change(screen.getByLabelText("Username"), {target: {value: "alice"}});
  fireEvent.change(screen.getByLabelText("Password"), {
    target: {value: "correct horse battery staple"},
  });
  fireEvent.click(screen.getByRole("button", {name: "Sign in"}));
}

describe("LoginPage", () => {
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
