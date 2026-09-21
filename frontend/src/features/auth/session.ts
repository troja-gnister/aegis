import {createContext, useContext, useSyncExternalStore} from "react";
import type {SessionResponse} from "./types";

export const SESSION_QUERY_KEY = ["auth", "session"] as const;

type SessionAccess = "open" | "signing_out" | "closed" | "unconfirmed" | "cleanup_failed";
let sessionAccess: SessionAccess = "open";
let accessGeneration = 0;
const accessListeners = new Set<() => void>();

export function isSessionAccessOpen(): boolean {
  return sessionAccess === "open";
}

function setAccess(value: SessionAccess) {
  sessionAccess = value;
  for (const listener of accessListeners) listener();
}

export function beginSignOut(): number {
  accessGeneration += 1;
  setAccess("signing_out");
  return accessGeneration;
}

export function completeSignOut(
  generation: number,
  confirmed: boolean,
  cleanupSucceeded = true,
): void {
  if (generation !== accessGeneration) return;
  setAccess(cleanupSucceeded ? (confirmed ? "closed" : "unconfirmed") : "cleanup_failed");
}

export function beginSignIn(): number {
  accessGeneration += 1;
  return accessGeneration;
}

export function captureSessionTransition(): number {
  return accessGeneration;
}

export function isSessionTransitionCurrent(generation: number): boolean {
  return generation === accessGeneration;
}

export function openSessionAfterLogin(): void {
  accessGeneration += 1;
  setAccess("open");
}

export function useSessionAccess(): SessionAccess {
  return useSyncExternalStore(
    (listener) => {
      accessListeners.add(listener);
      return () => accessListeners.delete(listener);
    },
    () => sessionAccess,
  );
}

export type AuthSessionContextValue = {
  session: SessionResponse;
};

export const AuthSessionContext = createContext<AuthSessionContextValue | null>(null);

export function useAuthSession(): AuthSessionContextValue {
  const value = useContext(AuthSessionContext);
  if (!value) throw new Error("Authenticated session context is unavailable");
  return value;
}
