import {createContext, useContext} from "react";
import type {SessionResponse} from "./types";

export const SESSION_QUERY_KEY = ["auth", "session"] as const;

export type AuthSessionContextValue = {
  session: SessionResponse;
};

export const AuthSessionContext = createContext<AuthSessionContextValue | null>(null);

export function useAuthSession(): AuthSessionContextValue {
  const value = useContext(AuthSessionContext);
  if (!value) throw new Error("Authenticated session context is unavailable");
  return value;
}
