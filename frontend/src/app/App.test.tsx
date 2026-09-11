import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {render, screen} from "@testing-library/react";
import {http, HttpResponse} from "msw";
import {MemoryRouter} from "react-router";
import {server} from "../test/server";
import {AppRoutes} from "./router";
import "../styles/global.css";

function renderAuthenticatedApp() {
  server.use(
    http.get("/api/v1/auth/session", () =>
      HttpResponse.json({
        user: {id: "abf81ca2-153b-4c7e-96b0-746082541c81", username: "alice"},
        cacheNamespace: "app-test-namespace".repeat(3),
      }),
    ),
    http.get("/api/v1/roots", () => HttpResponse.json({roots: []})),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({defaultOptions: {queries: {retry: false}}})}
    >
      <MemoryRouter initialEntries={["/roots"]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("renders the accessible dark authenticated application shell", async () => {
  renderAuthenticatedApp();
  expect(await screen.findByRole("banner")).toHaveTextContent("Aegis");
  await screen.findByRole("heading", {name: "No roots assigned"});
  expect(screen.getByRole("main")).toHaveTextContent("No roots assigned");
  expect(document.documentElement.dataset.theme).toBe("dark");
});

test("gives the Aegis home link a touch-sized inline target", async () => {
  renderAuthenticatedApp();

  const brand = await screen.findByRole("link", {name: "Aegis home"});
  expect(getComputedStyle(brand).minWidth).toBe("var(--touch-target)");
});
