import {describe, expect, it} from "vitest";

import {
  browserProjects,
  diagnosticProbeEnvironment,
  validatedWebkitEndpoint,
} from "./webkit-endpoint";

const valid = "ws://127.0.0.1:1024/0123456789abcdef0123456789abcdef";

describe("validatedWebkitEndpoint", () => {
  it("accepts one exact loopback WebKit endpoint", () => {
    expect(validatedWebkitEndpoint({E2E_WEBKIT_WS_ENDPOINT: valid})).toBe(valid);
    expect(validatedWebkitEndpoint({})).toBeUndefined();
  });

  it.each([
    "ws://localhost:1024/0123456789abcdef0123456789abcdef",
    "ws://127.0.0.1:1023/0123456789abcdef0123456789abcdef",
    "ws://127.0.0.1:65536/0123456789abcdef0123456789abcdef",
    "ws://127.0.0.1:01024/0123456789abcdef0123456789abcdef",
    "ws://user@127.0.0.1:1024/0123456789abcdef0123456789abcdef",
    "ws://127.0.0.1:1024/0123456789ABCDEF0123456789ABCDEF",
    "ws://127.0.0.1:1024/0123456789abcdef0123456789abcdef?token=x",
    "ws://127.0.0.1:1024/0123456789abcdef0123456789abcdef#fragment",
  ])("rejects unsafe endpoint %s", (endpoint) => {
    expect(() => validatedWebkitEndpoint({E2E_WEBKIT_WS_ENDPOINT: endpoint})).toThrow(
      "E2E_WEBKIT_WS_ENDPOINT is invalid",
    );
  });

  it.each([
    "PW_TEST_CONNECT_WS_ENDPOINT",
    "PW_TEST_CONNECT_HEADERS",
    "PW_TEST_CONNECT_EXPOSE_NETWORK",
  ])("rejects Playwright override %s while opted in", (name) => {
    expect(() => validatedWebkitEndpoint({
      E2E_WEBKIT_WS_ENDPOINT: valid,
      [name]: "",
    })).toThrow("Playwright internal connection variables are forbidden");
  });

  it.each([
    "PW_TEST_CONNECT_WS_ENDPOINT",
    "PW_TEST_CONNECT_HEADERS",
    "PW_TEST_CONNECT_EXPOSE_NETWORK",
  ])("rejects inherited Playwright override %s without opting in", (name) => {
    expect(() => validatedWebkitEndpoint({[name]: ""})).toThrow(
      "Playwright internal connection variables are forbidden",
    );
  });

  it("routes only WebKit remotely and keeps Chromium sandboxed", () => {
    expect(browserProjects(undefined)).toEqual([
      {name: "mobile-chromium", use: {browserName: "chromium"}},
      {name: "mobile-webkit", use: {browserName: "webkit"}},
    ]);
    expect(browserProjects(valid)).toEqual([
      {
        name: "mobile-chromium",
        use: {browserName: "chromium", launchOptions: {chromiumSandbox: true}},
      },
      {
        name: "mobile-webkit",
        use: {browserName: "webkit", connectOptions: {wsEndpoint: valid}},
      },
    ]);
  });
});

describe("diagnosticProbeEnvironment", () => {
  it("drops caller E2E values but keeps the WebKit connection for the probe runner", () => {
    expect(diagnosticProbeEnvironment({
      PATH: "/usr/bin",
      E2E_ALICE_PASSWORD: "caller-secret",
      E2E_BASE_URL: "http://127.0.0.1:18080",
      E2E_DIAGNOSTIC_MARKER: "/caller/marker-",
      E2E_WEBKIT_WS_ENDPOINT: valid,
    })).toEqual({PATH: "/usr/bin", E2E_WEBKIT_WS_ENDPOINT: valid});
    expect(diagnosticProbeEnvironment({
      PATH: "/usr/bin",
      E2E_ADMIN_PASSWORD: "caller-secret",
    })).toEqual({PATH: "/usr/bin"});
  });
});
