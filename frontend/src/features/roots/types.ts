export const ROOT_PERMISSION_NAMES = [
  "browse",
  "preview",
  "export",
  "create",
  "organize",
  "copy",
  "delete_restore",
  "root_admin",
] as const;

export type RootPermission = (typeof ROOT_PERMISSION_NAMES)[number];
export type RootMode = "read_only" | "read_write";

export type RootShell = {
  id: string;
  displayName: string;
  mode: RootMode;
  permissions: RootPermission[];
  authorizationEpoch: number;
};

export type RootListResponse = {
  roots: RootShell[];
};
