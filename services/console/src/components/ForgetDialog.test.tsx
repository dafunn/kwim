import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { renderWithProviders } from "../test/renderApp";
import { ForgetDialog } from "./ForgetDialog";

const PLAN_ITEM = {
  id: "fact-1",
  type: "fact",
  status: "current",
  label: "the sky is blue",
  evidence: ["ep-1"],
  episodics_to_delete: ["ep-1"],
  episodics_shared: [],
};

/** Preview-then-confirm, typed-count gate, and a 409 re-preview rather than a generic error or a resent token. */
describe("ForgetDialog", () => {
  it("cannot execute before a preview resolves, and stays disabled until the typed count matches", async () => {
    let previewCalls = 0;
    server.use(
      http.post("*/v1/admin/teams/:team/forget/preview", () => {
        previewCalls += 1;
        return HttpResponse.json({
          preview_token: "tok-1",
          expires_at: "2026-01-01T00:05:00Z",
          count: 1,
          plan: [PLAN_ITEM],
          preflight: { role: "kwim_user", can_delete: true },
        });
      }),
    );
    const user = userEvent.setup();

    renderWithProviders(
      <ForgetDialog team="acme" objectId="fact-1" objectType="fact" onClose={() => {}} onSuccess={() => {}} />,
      ["/"],
    );

    // Before the preview resolves, there is nothing to execute against.
    expect(screen.queryByRole("button", { name: /^forget$/i })).not.toBeInTheDocument();

    await waitFor(() => expect(screen.getByRole("button", { name: /^forget$/i })).toBeInTheDocument());
    const forgetButton = screen.getByRole("button", { name: /^forget$/i });
    expect(forgetButton).toBeDisabled();

    await user.type(screen.getByLabelText(/type 1 to enable/i), "9");
    expect(forgetButton).toBeDisabled();

    await user.clear(screen.getByLabelText(/type 1 to enable/i));
    await user.type(screen.getByLabelText(/type 1 to enable/i), "1");
    expect(forgetButton).toBeEnabled();

    expect(previewCalls).toBe(1);
  });

  it("surfaces a 409 on execute as a re-preview with the server's new count, and never resends the consumed token", async () => {
    let previewCount = 1;
    const tokensSeen: string[] = [];

    server.use(
      http.post("*/v1/admin/teams/:team/forget/preview", () =>
        HttpResponse.json({
          preview_token: `tok-${previewCount}`,
          expires_at: "2026-01-01T00:05:00Z",
          count: previewCount,
          plan: Array.from({ length: previewCount }, (_, i) => ({ ...PLAN_ITEM, id: `fact-${i}` })),
          preflight: { role: "kwim_user", can_delete: true },
        }),
      ),
      http.post("*/v1/admin/teams/:team/forget", async ({ request }) => {
        const body = (await request.json()) as { preview_token: string; confirm_count: number };
        tokensSeen.push(body.preview_token);
        if (body.preview_token === "tok-1") {
          previewCount = 2; // the set grew between preview and execute
          return HttpResponse.json(
            { detail: "confirm_count mismatch: 2 object(s) match now, submitted 1 - re-preview" },
            { status: 409 },
          );
        }
        return HttpResponse.json({ forgotten: previewCount, commit_log_rows: 1, episodic_events: 1, shared_skipped: [] });
      }),
    );
    const user = userEvent.setup();

    renderWithProviders(
      <ForgetDialog team="acme" objectId="fact-1" objectType="fact" onClose={() => {}} onSuccess={() => {}} />,
      ["/"],
    );

    await waitFor(() => expect(screen.getByText(/plan \(1 object\)/i)).toBeInTheDocument());
    await user.type(screen.getByLabelText(/type 1 to enable/i), "1");
    await user.click(screen.getByRole("button", { name: /^forget$/i }));

    // The 409 becomes a labeled re-preview prompt, not a generic error banner.
    await waitFor(() =>
      expect(screen.getByText(/changed since the last preview/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/plan \(2 objects\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/confirm_count mismatch/i)).not.toBeInTheDocument();

    // The stale token is never resent, and the old typed count doesn't carry over.
    expect(tokensSeen).toEqual(["tok-1"]);
    const input = screen.getByLabelText(/type 2 to enable/i) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByRole("button", { name: /^forget$/i })).toBeDisabled();
  });
});
