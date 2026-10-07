import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { renderWithProviders } from "../test/renderApp";
import { App } from "../App";

async function submit(username: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Username"), username);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: /log in/i }));
}

describe("Login", () => {
  it("redirects to the teams overview on success", async () => {
    server.use(
      http.post("/v1/admin/session", () =>
        HttpResponse.json({ token: "t", expires_at: "2026-01-01T00:00:00Z" }),
      ),
      http.get("/v1/admin/teams", () => HttpResponse.json({ items: [] })),
    );
    renderWithProviders(<App />, ["/login"]);

    await submit("alice", "correct-horse");

    await waitFor(() => expect(screen.getByRole("heading", { name: "Teams" })).toBeInTheDocument());
  });

  it("shows one non-disclosing message on invalid credentials", async () => {
    server.use(
      http.post("/v1/admin/session", () =>
        HttpResponse.json({ detail: "invalid username or password" }, { status: 401 }),
      ),
    );
    renderWithProviders(<App />, ["/login"]);

    await submit("alice", "wrong");

    expect(await screen.findByRole("alert")).toHaveTextContent("invalid username or password");
  });

  it("renders a distinct lockout state on 429", async () => {
    server.use(
      http.post("/v1/admin/session", () =>
        HttpResponse.json({ detail: "too many failed login attempts - try again later" }, { status: 429 }),
      ),
    );
    renderWithProviders(<App />, ["/login"]);

    await submit("alice", "wrong");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/too many failed login attempts/i);
  });

  it("renders a distinct disabled-console state on 503", async () => {
    server.use(
      http.post("/v1/admin/session", () =>
        HttpResponse.json(
          { detail: "admin console disabled - set KWIM_ADMIN_ENABLED=true to enable" },
          { status: 503 },
        ),
      ),
    );
    renderWithProviders(<App />, ["/login"]);

    await submit("alice", "whatever");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/admin console disabled/i);
  });
});
