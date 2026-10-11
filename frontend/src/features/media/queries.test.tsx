import {QueryClient} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, describe, expect, it, vi} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {isSessionAccessOpen} from "../auth/session";
import {entryQueryOptions} from "../files/queries";
import {mediaDetailsQueryOptions, mediaQueryKeys, requestMediaDerivativeForSession} from "./queries";
import {mediaDetailsBody} from "./test-fixtures";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const NAMESPACE = "media-query-namespace";
const clients = new Set<QueryClient>();

async function client() {
  const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
  clients.add(queryClient);
  await activateCacheNamespace(queryClient, NAMESPACE);
  return queryClient;
}

function unauthorized() {
  return HttpResponse.json({type: "authentication_required", title: "Authentication required"}, {status: 401});
}

afterEach(async () => {
  await Promise.all([...clients].map((queryClient) => purgePrivateBrowserState(queryClient)));
  clients.clear();
});

describe("media queries", () => {
  it("keys media details by namespace and entry without inactive retention", () => {
    const options = mediaDetailsQueryOptions(NAMESPACE, ENTRY_ID);
    expect(options.queryKey).toEqual(["media", NAMESPACE, "details", ENTRY_ID]);
    expect(options.queryKey.slice(0, 2)).toEqual(mediaQueryKeys.all(NAMESPACE));
    expect(mediaDetailsQueryOptions("other-namespace", ENTRY_ID).queryKey).not.toEqual(options.queryKey);
    expect(options.gcTime).toBe(0);
    expect(options.retry).toBe(false);
  });

  it("purges cached media details with the rest of the private state", async () => {
    const queryClient = await client();
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => HttpResponse.json(mediaDetailsBody(ENTRY_ID))));
    const options = {...mediaDetailsQueryOptions(NAMESPACE, ENTRY_ID), gcTime: 60_000};
    await queryClient.fetchQuery(options);
    expect(queryClient.getQueryData(options.queryKey)).toMatchObject({entryId: ENTRY_ID});

    await purgePrivateBrowserState(queryClient);

    expect(queryClient.getQueryData(options.queryKey)).toBeUndefined();
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})).toHaveLength(0);
  });

  it("does not run retained media options after their namespace is replaced", async () => {
    const queryClient = await client();
    const retained = mediaDetailsQueryOptions(NAMESPACE, ENTRY_ID);
    let requests = 0;
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => {
      requests += 1;
      return HttpResponse.json(mediaDetailsBody(ENTRY_ID));
    }));
    await activateCacheNamespace(queryClient, "replacement-namespace");

    await expect(queryClient.fetchQuery(retained)).rejects.toMatchObject({status: 0});
    expect(requests).toBe(0);
    expect(queryClient.getQueryData(retained.queryKey)).toBeUndefined();
  });

  it("closes access and purges private state on a media details 401", async () => {
    const queryClient = await client();
    queryClient.setQueryData(["media", NAMESPACE, "details", "retained"], "private");
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, unauthorized));

    await expect(queryClient.fetchQuery(mediaDetailsQueryOptions(NAMESPACE, ENTRY_ID))).rejects.toBeDefined();

    expect(isSessionAccessOpen()).toBe(false);
    expect(queryClient.getQueryCache().findAll({queryKey: ["media"]})).toHaveLength(0);
  });

  it("cancels media queries when a catalog request answers 401", async () => {
    const queryClient = await client();
    const cancelQueries = vi.spyOn(queryClient, "cancelQueries");
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}`, unauthorized));

    await expect(queryClient.fetchQuery(entryQueryOptions(NAMESPACE, ENTRY_ID))).rejects.toBeDefined();

    expect(cancelQueries).toHaveBeenCalledWith({queryKey: ["files"]});
    expect(cancelQueries).toHaveBeenCalledWith({queryKey: ["media"]});
    expect(isSessionAccessOpen()).toBe(false);
  });

  it("keeps the session open on a media details 404", async () => {
    const queryClient = await client();
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, () =>
      HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    ));
    await expect(queryClient.fetchQuery(mediaDetailsQueryOptions(NAMESPACE, ENTRY_ID)))
      .rejects.toMatchObject({status: 404});
    expect(isSessionAccessOpen()).toBe(true);
  });

  it("closes access and purges private state when a derivative request answers 401", async () => {
    const queryClient = await client();
    queryClient.setQueryData(["media", NAMESPACE, "details", ENTRY_ID], "private");
    server.use(http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, unauthorized));

    await expect(requestMediaDerivativeForSession(queryClient, NAMESPACE, ENTRY_ID, "preview"))
      .rejects.toMatchObject({status: 401});

    expect(isSessionAccessOpen()).toBe(false);
    expect(queryClient.getQueryData(["media", NAMESPACE, "details", ENTRY_ID])).toBeUndefined();
  });

  it("refuses a derivative request for a namespace that is not current", async () => {
    const queryClient = await client();
    let requests = 0;
    server.use(http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, () => {
      requests += 1;
      return HttpResponse.json({state: "pending", kind: "preview"}, {status: 202});
    }));

    await expect(requestMediaDerivativeForSession(queryClient, "stale-namespace", ENTRY_ID, "preview"))
      .rejects.toMatchObject({status: 0});
    expect(requests).toBe(0);
  });
});
