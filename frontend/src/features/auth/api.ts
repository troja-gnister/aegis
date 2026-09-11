import {apiRequest} from "../../api/http";
import {ApiProblem, genericApiProblem} from "../../api/problem";
import type {
  AuthenticatedUser,
  CsrfResponse,
  LoginResponse,
  SessionResponse,
} from "./types";

let csrfToken: string | null = null;

function isBoundedString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function validatedUser(value: unknown): AuthenticatedUser {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw genericApiProblem(502);
  }
  const fields = value as Record<string, unknown>;
  if (!isBoundedString(fields.id, 64) || !isBoundedString(fields.username, 150)) {
    throw genericApiProblem(502);
  }
  return {id: fields.id, username: fields.username};
}

function validatedSession(value: unknown): SessionResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw genericApiProblem(502);
  }
  const fields = value as Record<string, unknown>;
  if (!isBoundedString(fields.cacheNamespace, 128)) {
    throw genericApiProblem(502);
  }
  return {
    user: validatedUser(fields.user),
    cacheNamespace: fields.cacheNamespace,
  };
}

export function clearCsrfToken(): void {
  csrfToken = null;
}

async function refreshCsrfToken(): Promise<string> {
  const response = await apiRequest<CsrfResponse>("/api/v1/auth/csrf");
  if (!isBoundedString(response.csrfToken, 256)) throw genericApiProblem(502);
  csrfToken = response.csrfToken;
  return csrfToken;
}

async function csrfMutation<T>(path: string, body?: unknown): Promise<T> {
  const initialToken = csrfToken ?? (await refreshCsrfToken());
  try {
    return await apiRequest<T>(path, {method: "POST", body, csrfToken: initialToken});
  } catch (error) {
    if (!(error instanceof ApiProblem) || error.type !== "csrf_failed") throw error;
    const replacementToken = await refreshCsrfToken();
    return apiRequest<T>(path, {
      method: "POST",
      body,
      csrfToken: replacementToken,
    });
  }
}

export async function fetchSession(): Promise<SessionResponse> {
  return validatedSession(await apiRequest<unknown>("/api/v1/auth/session"));
}

export async function loginWithCredentials(
  username: string,
  password: string,
): Promise<LoginResponse> {
  const response = await csrfMutation<unknown>("/api/v1/auth/login", {
    username,
    password,
  });
  clearCsrfToken();
  if (!response || typeof response !== "object" || Array.isArray(response)) {
    throw genericApiProblem(502);
  }
  return {user: validatedUser((response as Record<string, unknown>).user)};
}

export async function logoutSession(): Promise<void> {
  await csrfMutation<void>("/api/v1/auth/logout");
  clearCsrfToken();
}
