import type {QueryClient} from "@tanstack/react-query";
import {clearCsrfToken} from "./api";

export const AEGIS_DATABASE_NAMES = [] as const;
const trackedObjectUrls = new Set<string>();
const privateStateCleanups = new Set<() => void>();
let activeCacheNamespace: string | null = null;
let privateStateGeneration = 0;

export type PrivateStateSnapshot = Readonly<{
  generation: number;
  namespace: string | null;
}>;

export class PrivateStateCleanupError extends Error {
  constructor() {
    super("Private browser state cleanup failed");
    this.name = "PrivateStateCleanupError";
  }
}

export function registerPrivateStateCleanup(cleanup: () => void): () => void {
  privateStateCleanups.add(cleanup);
  return () => privateStateCleanups.delete(cleanup);
}

export function capturePrivateState(): PrivateStateSnapshot {
  return {generation: privateStateGeneration, namespace: activeCacheNamespace};
}

export function isPrivateStateCurrent(
  snapshot: PrivateStateSnapshot,
  namespace = snapshot.namespace,
): boolean {
  return snapshot.generation === privateStateGeneration &&
    namespace !== null && activeCacheNamespace === namespace;
}

function clearRegisteredPrivateState(): boolean {
  let failed = false;
  for (const cleanup of [...privateStateCleanups]) {
    try {
      cleanup();
    } catch {
      failed = true;
    }
  }
  return failed;
}

export function createTrackedObjectUrl(value: Blob | MediaSource): string {
  const url = URL.createObjectURL(value);
  trackedObjectUrls.add(url);
  return url;
}

export function releaseTrackedObjectUrl(url: string): void {
  if (!trackedObjectUrls.delete(url)) return;
  URL.revokeObjectURL(url);
}

function revokeTrackedObjectUrls(): void {
  for (const url of trackedObjectUrls) URL.revokeObjectURL(url);
  trackedObjectUrls.clear();
}

function deleteIndexedDatabase(name: string): Promise<void> {
  if (!("indexedDB" in window)) return Promise.resolve();
  return new Promise((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = window.indexedDB.deleteDatabase(name);
    } catch {
      resolve();
      return;
    }
    request.addEventListener("success", () => resolve(), {once: true});
    request.addEventListener("error", () => resolve(), {once: true});
    request.addEventListener("blocked", () => resolve(), {once: true});
  });
}

async function clearAegisCacheStorage(): Promise<void> {
  if (!("caches" in window)) return;
  try {
    const names = await window.caches.keys();
    await Promise.allSettled(
      names
        .filter((name) => name.startsWith("aegis-"))
        .map((name) => window.caches.delete(name)),
    );
  } catch {
    return;
  }
}

async function unregisterAegisServiceWorkers(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  try {
    const appScope = new URL(import.meta.env.BASE_URL, window.location.origin).href;
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.allSettled(
      registrations
        .filter((registration) => registration.scope.startsWith(appScope))
        .map((registration) => registration.unregister()),
    );
  } catch {
    return;
  }
}

export async function purgePrivateBrowserState(queryClient: QueryClient): Promise<void> {
  privateStateGeneration += 1;
  const ownerCleanupFailed = clearRegisteredPrivateState();
  queryClient.clear();
  clearCsrfToken();
  activeCacheNamespace = null;
  revokeTrackedObjectUrls();

  await Promise.all([
    clearAegisCacheStorage(),
    Promise.all(AEGIS_DATABASE_NAMES.map(deleteIndexedDatabase)),
    unregisterAegisServiceWorkers(),
  ]);
  if (ownerCleanupFailed) throw new PrivateStateCleanupError();
}

export async function activateCacheNamespace(
  queryClient: QueryClient,
  namespace: string,
  isCurrent: () => boolean = () => true,
): Promise<boolean> {
  if (!isCurrent()) return false;
  const namespaceChanged = activeCacheNamespace !== namespace;
  const changed = activeCacheNamespace !== null && activeCacheNamespace !== namespace;
  if (changed) await purgePrivateBrowserState(queryClient);
  // Browser cleanup can finish after another transition owns the namespace.
  if (!isCurrent()) return false;
  activeCacheNamespace = namespace;
  if (namespaceChanged) privateStateGeneration += 1;
  return changed;
}
