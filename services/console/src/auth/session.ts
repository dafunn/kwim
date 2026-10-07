import { client, unwrap } from "../api/client";
import type { components } from "../api/schema";

export type LoginCredentials = components["schemas"]["AdminLoginRequest"];
export type Session = components["schemas"]["AdminSessionResponse"];

/**
 * POST /v1/admin/session sets the HttpOnly cookie and also returns
 * {token, expires_at} for scripted callers - the console ignores the token
 * field and relies on the cookie alone.
 */
export async function login(credentials: LoginCredentials): Promise<Session> {
  return unwrap(client.POST("/v1/admin/session", { body: credentials }));
}

export async function logout(): Promise<void> {
  await client.DELETE("/v1/admin/session");
}

/**
 * The router registers `navigate` here so the 401 handler can redirect; falls
 * back to a full page load before the router mounts.
 */
let navigate: ((to: string) => void) | null = null;

export function setNavigator(fn: (to: string) => void): void {
  navigate = fn;
}

export function redirectToLogin(): void {
  if (navigate) {
    navigate("/login");
  } else {
    window.location.assign("/login");
  }
}
