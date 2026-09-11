import {http, HttpResponse} from "msw";

export const handlers = [
  http.get("/api/v1/auth/csrf", () =>
    HttpResponse.json({csrfToken: "csrf-test-token"}),
  ),
  http.get("/api/v1/auth/session", () =>
    HttpResponse.json(
      {type: "authentication_required", title: "Authentication required"},
      {status: 401},
    ),
  ),
];
