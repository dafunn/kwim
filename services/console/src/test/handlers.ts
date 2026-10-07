import { http, HttpResponse } from "msw";

/**
 * Logged-out defaults: /health succeeds and /v1/admin/* answers 401 unless a
 * test overrides it with server.use(...).
 */
export const handlers = [
  http.get("/health", () =>
    HttpResponse.json({ status: "ok", service: "kwim", version: "0.2.0" }),
  ),
  http.all("/v1/admin/*", () =>
    HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
  ),
];
