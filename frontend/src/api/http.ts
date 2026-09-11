import {ApiProblem, genericApiProblem} from "./problem";

const MAX_PROBLEM_BYTES = 16 * 1024;
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const PROBLEM_TYPE = /^[a-z][a-z0-9_]{0,63}$/;

export type ApiRequestInit = Omit<
  RequestInit,
  "body" | "credentials" | "redirect"
> & {
  body?: unknown;
  csrfToken?: string;
};

function validatedApiPath(path: string): string {
  if (!path.startsWith("/api/v1/") || path.startsWith("//")) {
    throw new Error("API path must be same-origin and versioned");
  }
  const resolved = new URL(path, window.location.origin);
  if (
    resolved.origin !== window.location.origin ||
    !resolved.pathname.startsWith("/api/v1/")
  ) {
    throw new Error("API path must be same-origin and versioned");
  }
  return `${resolved.pathname}${resolved.search}`;
}

async function readBoundedText(response: Response): Promise<string | null> {
  if (!response.body) return "";

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_PROBLEM_BYTES) {
        void reader.cancel().catch(() => undefined);
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", {fatal: true}).decode(bytes);
}

function boundedRetryAfter(response: Response, bodyValue: unknown): number | undefined {
  const headerValue = response.headers.get("Retry-After");
  const candidate = headerValue === null ? bodyValue : Number(headerValue);
  return typeof candidate === "number" &&
    Number.isInteger(candidate) &&
    candidate >= 0 &&
    candidate <= 86_400
    ? candidate
    : undefined;
}

function containsControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint <= 31 || codePoint === 127;
  });
}

export async function parseBoundedProblem(response: Response): Promise<ApiProblem> {
  const contentType = response.headers.get("Content-Type")?.toLowerCase() ?? "";
  if (!contentType.includes("application/json")) {
    return genericApiProblem(response.status);
  }

  try {
    const text = await readBoundedText(response);
    if (text === null) return genericApiProblem(response.status);
    const candidate: unknown = JSON.parse(text);
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
      return genericApiProblem(response.status);
    }
    const fields = candidate as Record<string, unknown>;
    if (
      typeof fields.type !== "string" ||
      !PROBLEM_TYPE.test(fields.type) ||
      typeof fields.title !== "string" ||
      fields.title.length < 1 ||
      fields.title.length > 160 ||
      containsControlCharacter(fields.title)
    ) {
      return genericApiProblem(response.status);
    }
    return new ApiProblem({
      type: fields.type,
      title: fields.title,
      status: response.status,
      retryAfterSeconds: boundedRetryAfter(response, fields.retryAfterSeconds),
    });
  } catch {
    return genericApiProblem(response.status);
  }
}

export async function apiRequest<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const url = validatedApiPath(path);
  const {body, csrfToken, ...requestInit} = init;
  const method = (requestInit.method ?? "GET").toUpperCase();
  const headers = new Headers(requestInit.headers);
  headers.delete("Authorization");
  headers.set("Accept", "application/json");
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (!SAFE_METHODS.has(method) && csrfToken) {
    headers.set("X-CSRFToken", csrfToken);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...requestInit,
      method,
      headers,
      credentials: "same-origin",
      redirect: "error",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw genericApiProblem();
  }

  if (
    response.redirected ||
    (response.url && new URL(response.url).origin !== window.location.origin)
  ) {
    throw genericApiProblem(response.status);
  }
  if (!response.ok) throw await parseBoundedProblem(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
