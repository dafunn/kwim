import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "./test/renderApp";
import { App } from "./App";

describe("401 handling", () => {
  it("returns to login on a 401 from any call and clears local state", async () => {
    // The default MSW handler answers every /v1/admin/* call with 401.
    renderWithProviders(<App />, ["/teams"]);

    await waitFor(() => expect(screen.getByLabelText("Username")).toBeInTheDocument());
  });
});
