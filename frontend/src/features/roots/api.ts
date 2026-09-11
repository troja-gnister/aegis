import {apiRequest} from "../../api/http";
import {genericApiProblem} from "../../api/problem";
import {
  ROOT_PERMISSION_NAMES,
  type RootListResponse,
  type RootMode,
  type RootPermission,
  type RootShell,
} from "./types";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const PERMISSIONS = new Set<string>(ROOT_PERMISSION_NAMES);

function validatedRoot(value: unknown): RootShell {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw genericApiProblem(502);
  }
  const fields = value as Record<string, unknown>;
  if (
    typeof fields.id !== "string" ||
    !UUID.test(fields.id) ||
    typeof fields.displayName !== "string" ||
    fields.displayName.length < 1 ||
    fields.displayName.length > 160 ||
    (fields.mode !== "read_only" && fields.mode !== "read_write") ||
    !Array.isArray(fields.permissions) ||
    fields.permissions.length > ROOT_PERMISSION_NAMES.length ||
    !fields.permissions.every(
      (permission) => typeof permission === "string" && PERMISSIONS.has(permission),
    ) ||
    new Set(fields.permissions).size !== fields.permissions.length ||
    !Number.isSafeInteger(fields.authorizationEpoch) ||
    (fields.authorizationEpoch as number) < 0
  ) {
    throw genericApiProblem(502);
  }
  return {
    id: fields.id,
    displayName: fields.displayName,
    mode: fields.mode as RootMode,
    permissions: fields.permissions as RootPermission[],
    authorizationEpoch: fields.authorizationEpoch as number,
  };
}

export async function fetchRoots(): Promise<RootListResponse> {
  const response = await apiRequest<unknown>("/api/v1/roots");
  if (!response || typeof response !== "object" || Array.isArray(response)) {
    throw genericApiProblem(502);
  }
  const roots = (response as Record<string, unknown>).roots;
  if (!Array.isArray(roots) || roots.length > 4096) throw genericApiProblem(502);
  return {roots: roots.map(validatedRoot)};
}
