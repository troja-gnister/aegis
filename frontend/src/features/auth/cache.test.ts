import {QueryClient} from "@tanstack/react-query";
import {afterEach, describe, expect, it, vi} from "vitest";
import {
  activateCacheNamespace,
  capturePrivateState,
  createTrackedObjectUrl,
  isPrivateStateCurrent,
  purgePrivateBrowserState,
  registerPrivateStateCleanup,
} from "./cache";

const originalCaches = Object.getOwnPropertyDescriptor(window, "caches");
const originalServiceWorker = Object.getOwnPropertyDescriptor(
  navigator,
  "serviceWorker",
);

afterEach(() => {
  vi.restoreAllMocks();
  if (originalCaches) Object.defineProperty(window, "caches", originalCaches);
  else Reflect.deleteProperty(window, "caches");
  if (originalServiceWorker) {
    Object.defineProperty(navigator, "serviceWorker", originalServiceWorker);
  } else {
    Reflect.deleteProperty(navigator, "serviceWorker");
  }
});

describe("purgePrivateBrowserState", () => {
  it("keeps in-flight ownership current when the same namespace is reactivated", async () => {
    const queryClient = new QueryClient();
    await activateCacheNamespace(queryClient, "stable-account");
    const snapshot = capturePrivateState();

    await activateCacheNamespace(queryClient, "stable-account");

    expect(isPrivateStateCurrent(snapshot, "stable-account")).toBe(true);
    await purgePrivateBrowserState(queryClient);
  });

  it("runs registered private cleanup synchronously and supports owner disposal", async () => {
    const queryClient = new QueryClient();
    const cleanup = vi.fn();
    const unsubscribe = registerPrivateStateCleanup(cleanup);
    let release = () => {};
    const gate = new Promise<string[]>((resolve) => { release = () => resolve([]); });
    Object.defineProperty(window, "caches", {
      configurable: true,
      value: {keys: () => gate},
    });

    const pending = purgePrivateBrowserState(queryClient);
    expect(cleanup).toHaveBeenCalledOnce();
    release();
    await pending;

    unsubscribe();
    await purgePrivateBrowserState(queryClient);
    expect(cleanup).toHaveBeenCalledOnce();
  });

  it("does not let obsolete namespace cleanup replace a newer account namespace", async () => {
    const client = new QueryClient();
    await purgePrivateBrowserState(client);
    await activateCacheNamespace(client, "initial-account");
    let release = () => {};
    const gate = new Promise<string[]>((resolve) => { release = () => resolve([]); });
    Object.defineProperty(window, "caches", {
      configurable: true,
      value: {keys: () => gate},
    });
    let current = true;
    const obsoleteActivation = activateCacheNamespace(client, "obsolete-account", () => current);
    current = false;
    await activateCacheNamespace(client, "current-account");
    client.setQueryData(["private", "current-account"], "fresh private selection");
    release();
    await obsoleteActivation;
    await activateCacheNamespace(client, "current-account");

    expect(client.getQueryData(["private", "current-account"])).toBe("fresh private selection");
    await purgePrivateBrowserState(client);
  });

  it("clears private queries, object URLs, Aegis caches, and app registrations", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["private", "root"], "secret");
    const revokeObjectUrl = vi.fn();
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:aegis-private");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(revokeObjectUrl);
    createTrackedObjectUrl(new Blob(["private"]));

    const deleteCache = vi.fn(async () => true);
    Object.defineProperty(window, "caches", {
      configurable: true,
      value: {
        keys: vi.fn(async () => ["aegis-thumbnails", "unrelated-cache"]),
        delete: deleteCache,
      },
    });
    const unregisterApp = vi.fn(async () => true);
    const unregisterElsewhere = vi.fn(async () => true);
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        getRegistrations: vi.fn(async () => [
          {scope: "http://localhost/app/", unregister: unregisterApp},
          {scope: "https://elsewhere.test/", unregister: unregisterElsewhere},
        ]),
      },
    });

    await purgePrivateBrowserState(queryClient);

    expect(queryClient.getQueryData(["private", "root"])).toBeUndefined();
    expect(revokeObjectUrl).toHaveBeenCalledExactlyOnceWith("blob:aegis-private");
    expect(deleteCache).toHaveBeenCalledExactlyOnceWith("aegis-thumbnails");
    expect(unregisterApp).toHaveBeenCalledOnce();
    expect(unregisterElsewhere).not.toHaveBeenCalled();
  });

  it("keeps local state fail-closed when optional browser cleanup rejects", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["private", "root"], "secret");
    Object.defineProperty(window, "caches", {
      configurable: true,
      value: {keys: vi.fn(async () => Promise.reject(new Error("cache unavailable")))},
    });

    await expect(purgePrivateBrowserState(queryClient)).resolves.toBeUndefined();
    expect(queryClient.getQueryData(["private", "root"])).toBeUndefined();
  });
});
