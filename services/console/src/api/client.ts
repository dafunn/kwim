import createClient from "openapi-fetch";
import type { paths } from "./schema";

/**
 * Same-origin base (fetch needs an absolute URL). `credentials: "same-origin"`
 * sends the HttpOnly session cookie.
 */
export const client = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: "same-origin",
  // Looked up on each call, so a fetch patched later (MSW in tests) is used.
  fetch: (...args: Parameters<typeof globalThis.fetch>) => globalThis.fetch(...args),
});

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function extractDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const { detail } = error as { detail: unknown };
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

/**
 * Unwraps an openapi-fetch result into its data or throws ApiError. Many admin
 * responses have no declared model, so callers narrow `unknown` payloads to the
 * shapes in services/api/tests/test_admin_*.py.
 */
export async function unwrap<T>(
  result: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await result;
  if (error !== undefined) {
    throw new ApiError(response.status, extractDetail(error, response.statusText));
  }
  return data as T;
}
