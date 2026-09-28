import {QueryClient} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {logoutSession, obtainCsrfToken} from "./api";
import {
  activateCacheNamespace,
  capturePrivateState,
  isPrivateStateCurrent,
  purgePrivateBrowserState,
} from "./cache";

const clients = new Set<QueryClient>();

async function client(namespace: string): Promise<QueryClient> {
  const queryClient = new QueryClient();
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, namespace);
  return queryClient;
}

function deferredCsrf(tokens: string[]) {
  const releases: Array<() => void> = [];
  let calls = 0;
  server.use(http.get("/api/v1/auth/csrf", async () => {
    const token = tokens[calls] ?? `csrf-${calls}`;
    calls += 1;
    await new Promise<void>((resolve) => releases.push(resolve));
    return HttpResponse.json({csrfToken: token});
  }));
  return {
    calls: () => calls,
    async release(index: number) {
      while (releases.length <= index) await new Promise((resolve) => setTimeout(resolve, 1));
      releases[index]!();
    },
  };
}

function immediateCsrf(tokens: string[]) {
  let calls = 0;
  server.use(http.get("/api/v1/auth/csrf", () => {
    const token = tokens[calls] ?? `csrf-${calls}`;
    calls += 1;
    return HttpResponse.json({csrfToken: token});
  }));
  return () => calls;
}

afterEach(async () => {
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("shared CSRF store", () => {
  it("reuses the one existing CSRF token for later mutations", async () => {
    await client("account-a");
    const calls = immediateCsrf(["csrf-a"]);
    const snapshot = capturePrivateState();
    const isCurrent = () => isPrivateStateCurrent(snapshot);
    await expect(obtainCsrfToken(isCurrent)).resolves.toBe("csrf-a");
    await expect(obtainCsrfToken(isCurrent)).resolves.toBe("csrf-a");
    expect(calls()).toBe(1);
  });

  it("replaces only a token the server rejected", async () => {
    await client("account-a");
    const calls = immediateCsrf(["csrf-a", "csrf-a2"]);
    const isCurrent = () => true;
    await expect(obtainCsrfToken(isCurrent)).resolves.toBe("csrf-a");
    await expect(obtainCsrfToken(isCurrent, "csrf-a")).resolves.toBe("csrf-a2");
    await expect(obtainCsrfToken(isCurrent, "csrf-a")).resolves.toBe("csrf-a2");
    expect(calls()).toBe(2);
  });

  it("refuses to start acquisition for an obsolete initiator", async () => {
    await client("account-a");
    const calls = immediateCsrf(["csrf-a"]);
    await expect(obtainCsrfToken(() => false)).rejects.toMatchObject({status: 0});
    expect(calls()).toBe(0);
  });

  it("rejects an acquisition that completes after logout and never stores its token", async () => {
    const queryClient = await client("account-a");
    const csrf = deferredCsrf(["csrf-a", "csrf-b"]);
    const pending = obtainCsrfToken(() => true);
    await purgePrivateBrowserState(queryClient);
    await activateCacheNamespace(queryClient, "account-b");
    await csrf.release(0);
    await expect(pending).rejects.toMatchObject({status: 0});

    const next = obtainCsrfToken(() => true);
    await csrf.release(1);
    await expect(next).resolves.toBe("csrf-b");
    expect(csrf.calls()).toBe(2);
  });

  it("rejects an acquisition whose initiating namespace was replaced by another account", async () => {
    const queryClient = await client("account-a");
    const csrf = deferredCsrf(["csrf-a"]);
    const snapshot = capturePrivateState();
    const pending = obtainCsrfToken(() => isPrivateStateCurrent(snapshot, "account-a"));
    await activateCacheNamespace(queryClient, "account-b");
    await csrf.release(0);
    await expect(pending).rejects.toMatchObject({status: 0});
  });

  it("does not let a logout's in-flight token acquisition repopulate the store after purge", async () => {
    const queryClient = await client("account-a");
    const csrf = deferredCsrf(["csrf-a", "csrf-b"]);
    const tokens: Array<string | null> = [];
    let releaseLogout = () => {};
    const logoutGate = new Promise<void>((resolve) => { releaseLogout = resolve; });
    server.use(http.post("/api/v1/auth/logout", async ({request}) => {
      tokens.push(request.headers.get("X-CSRFToken"));
      await logoutGate;
      return new HttpResponse(null, {status: 204});
    }));
    const logout = logoutSession();
    await purgePrivateBrowserState(queryClient);
    await activateCacheNamespace(queryClient, "account-b");
    await csrf.release(0);
    while (tokens.length === 0) await new Promise((resolve) => setTimeout(resolve, 1));
    // The revoking request keeps its own initiating token.
    expect(tokens).toEqual(["csrf-a"]);

    // While that request is still pending, the next account must not borrow it.
    const next = obtainCsrfToken(() => true);
    await csrf.release(1);
    await expect(next).resolves.toBe("csrf-b");
    expect(csrf.calls()).toBe(2);
    releaseLogout();
    await logout;
  });
});
