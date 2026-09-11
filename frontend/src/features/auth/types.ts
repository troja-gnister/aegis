export type AuthenticatedUser = {
  id: string;
  username: string;
};

export type SessionResponse = {
  user: AuthenticatedUser;
  cacheNamespace: string;
};

export type CsrfResponse = {
  csrfToken: string;
};

export type LoginResponse = {
  user: AuthenticatedUser;
};
