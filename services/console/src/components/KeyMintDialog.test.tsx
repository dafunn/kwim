import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { renderWithProviders } from "../test/renderApp";
import { KeyMintDialog } from "./KeyMintDialog";

/** The secret is shown once, and nothing writes it to localStorage or sessionStorage. */
describe("KeyMintDialog", () => {
  it("shows the minted secret once and touches neither localStorage nor sessionStorage", async () => {
    server.use(
      http.post("*/v1/admin/teams/:team/keys", () =>
        HttpResponse.json({
          id: "key-1",
          key_prefix: "kw_abc123",
          capabilities: ["read"],
          key: "kw_abc123.secret",
        }),
      ),
    );
    const user = userEvent.setup();
    localStorage.clear();
    sessionStorage.clear();

    renderWithProviders(<KeyMintDialog team="acme" onClose={() => {}} />, ["/"]);

    await user.type(screen.getByLabelText("Label"), "ci key");
    await user.click(screen.getByRole("checkbox", { name: "read" }));
    await user.click(screen.getByRole("button", { name: /mint key/i }));

    await waitFor(() => expect(screen.getByText("kw_abc123.secret")).toBeInTheDocument());
    expect(screen.getByText(/shown once, right now/i)).toBeInTheDocument();

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
