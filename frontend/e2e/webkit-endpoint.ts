const INTERNAL_ENDPOINT_VARIABLES = [
  "PW_TEST_CONNECT_WS_ENDPOINT",
  "PW_TEST_CONNECT_HEADERS",
  "PW_TEST_CONNECT_EXPOSE_NETWORK",
] as const;

export function validatedWebkitEndpoint(
  environment: NodeJS.ProcessEnv,
): string | undefined {
  if (INTERNAL_ENDPOINT_VARIABLES.some((name) => name in environment)) {
    throw new Error("Playwright internal connection variables are forbidden");
  }
  if (!("E2E_WEBKIT_WS_ENDPOINT" in environment)) return undefined;
  const endpoint = environment.E2E_WEBKIT_WS_ENDPOINT ?? "";
  const match = /^ws:\/\/127\.0\.0\.1:([1-9][0-9]{3,4})\/([0-9a-f]{32})$/.exec(
    endpoint,
  );
  if (match === null) throw new Error("E2E_WEBKIT_WS_ENDPOINT is invalid");
  const port = Number(match[1]);
  if (port < 1024 || port > 65535 || String(port) !== match[1]) {
    throw new Error("E2E_WEBKIT_WS_ENDPOINT is invalid");
  }
  return endpoint;
}

// The credential-diagnostic probe supplies its own synthetic E2E values. Every caller E2E
// value is dropped except the WebKit connection (validated again by the probe's config),
// so each mobile browser runs where the real gate runs it.
export function diagnosticProbeEnvironment(
  environment: NodeJS.ProcessEnv,
): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(environment).filter(
    ([name]) => !name.startsWith("E2E_") || name === "E2E_WEBKIT_WS_ENDPOINT",
  ));
}

export function browserProjects(endpoint: string | undefined) {
  return [
    {
      name: "mobile-chromium",
      use: {
        browserName: "chromium" as const,
        ...(endpoint === undefined
          ? {}
          : {launchOptions: {chromiumSandbox: true}}),
      },
    },
    {
      name: "mobile-webkit",
      use: {
        browserName: "webkit" as const,
        ...(endpoint === undefined
          ? {}
          : {connectOptions: {wsEndpoint: endpoint}}),
      },
    },
  ];
}
