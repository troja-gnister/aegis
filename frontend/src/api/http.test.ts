import {http, HttpResponse} from "msw";
import {describe, expect, it, vi} from "vitest";
import {ApiProblem} from "./problem";
import {apiRequest} from "./http";
import {server} from "../test/server";

describe("apiRequest", () => {
  it("sends same-origin credentials and CSRF without bearer state", async () => {
    const localWrite = vi.spyOn(Storage.prototype, "setItem");
    server.use(
      http.post("/api/v1/auth/logout", ({request}) => {
        expect(request.credentials).toBe("same-origin");
        expect(request.headers.get("X-CSRFToken")).toBe("csrf-test-token");
        expect(request.headers.get("Authorization")).toBeNull();
        return new HttpResponse(null, {status: 204});
      }),
    );

    await apiRequest<void>("/api/v1/auth/logout", {
      method: "POST",
      csrfToken: "csrf-test-token",
      headers: {Authorization: "Bearer forbidden"},
    });

    expect(localWrite).not.toHaveBeenCalled();
  });

  it.each(["https://example.test/api/v1/auth/session", "//example.test/api/v1/x", "/health/live"])(
    "rejects a non-versioned same-origin path: %s",
    async (path) => {
      await expect(apiRequest(path)).rejects.toThrow(
        "API path must be same-origin and versioned",
      );
    },
  );

  it.each([
    [401, "authentication_required"],
    [403, "csrf_failed"],
    [429, "login_throttled"],
  ])("returns a typed bounded problem for HTTP %i", async (status, type) => {
    server.use(
      http.get("/api/v1/problem", () =>
        HttpResponse.json(
          {type, title: "Safe title", status, ignored: "never exposed"},
          {status, headers: status === 429 ? {"Retry-After": "19"} : {}},
        ),
      ),
    );

    const problem = await apiRequest("/api/v1/problem").catch((error: unknown) => error);

    expect(problem).toBeInstanceOf(ApiProblem);
    expect(problem).toMatchObject({type, title: "Safe title", status});
    expect((problem as ApiProblem).retryAfterSeconds).toBe(
      status === 429 ? 19 : undefined,
    );
    expect(problem).not.toHaveProperty("ignored");
  });

  it.each([
    ["text/html", "<h1>private upstream detail</h1>"],
    ["application/json", JSON.stringify({title: "x".repeat(20_000)})],
  ])("maps an unsafe %s error body to a stable generic problem", async (contentType, body) => {
    server.use(
      http.get(
        "/api/v1/unsafe-error",
        () =>
          new HttpResponse(body, {
            status: 502,
            headers: {"Content-Type": contentType},
          }),
      ),
    );

    await expect(apiRequest("/api/v1/unsafe-error")).rejects.toMatchObject({
      type: "request_failed",
      title: "Request failed",
      status: 502,
    });
  });
});
