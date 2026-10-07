import { ApiError } from "./client";

/** The server's detail message for any error, including 403 and 409. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  return "Something went wrong.";
}
