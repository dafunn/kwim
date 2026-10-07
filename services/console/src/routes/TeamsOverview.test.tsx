import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { Route, Routes } from "react-router-dom";
import { server } from "../test/server";
import { renderWithProviders } from "../test/renderApp";
import { TeamsOverview } from "./TeamsOverview";

describe("TeamsOverview provisioning drift", () => {
  it("flags a schema with no console record, and a console record with no schema", async () => {
    server.use(
      http.get("*/v1/admin/teams", () =>
        HttpResponse.json({
          items: [
            {
              team: "orphan-schema",
              display_name: null,
              status: null,
              created_at: null,
              has_schema: true,
              has_console_record: false,
              counts: null,
            },
            {
              team: "dead-record",
              display_name: "Dead Record",
              status: "active",
              created_at: "2026-01-01T00:00:00Z",
              has_schema: false,
              has_console_record: true,
              counts: { facts: 0, rules: 0, semantic: 0, episodic: 0, pending: 0 },
            },
          ],
        }),
      ),
    );

    renderWithProviders(
      <Routes>
        <Route path="/teams" element={<TeamsOverview />} />
      </Routes>,
      ["/teams"],
    );

    await waitFor(() => expect(screen.getByText("schema, not adopted")).toBeInTheDocument());
    expect(screen.getByText("console record, no schema")).toBeInTheDocument();
  });
});
