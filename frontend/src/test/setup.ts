import "@testing-library/jest-dom/vitest";
import {afterAll, afterEach, beforeAll} from "vitest";
import {server} from "./server";
import {openSessionAfterLogin} from "../features/auth/session";

beforeAll(() => server.listen({onUnhandledRequest: "error"}));
afterEach(() => {
  server.resetHandlers();
  openSessionAfterLogin();
});
afterAll(() => server.close());
