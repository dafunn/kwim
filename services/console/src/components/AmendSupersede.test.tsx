import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../test/renderApp";
import { AmendFactDialog } from "./AmendFactDialog";
import { SupersedeFactDialog } from "./SupersedeFactDialog";
import type { Fact } from "../api/facts";

const fact: Fact = {
  id: "fact-1",
  statement: "the sky is blue",
  fact_type: "observation",
  status: "current",
  created_at: "2026-01-01T00:00:00Z",
  about: [],
  decay_class: "slow",
  as_of: "2026-01-01T00:00:00Z",
  freshness: "fresh",
  last_verified_at: null,
  source_kind: null,
};

/** Amend and supersede are distinct dialogs with distinct wording, and neither submits without a reason. */
describe("Amend and supersede dialogs", () => {
  it("Amend reads as 'correct this entry' and disables submit until a reason is typed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AmendFactDialog team="acme" fact={fact} onClose={() => {}} onSuccess={() => {}} />, ["/"]);

    expect(screen.getByText(/replaces the current text in place/i)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Amend" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/reason/i), "typo fix");
    expect(submit).toBeEnabled();
  });

  it("Supersede reads as 'replace with a new version' and disables submit until a reason is typed", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <SupersedeFactDialog team="acme" fact={fact} onClose={() => {}} onSuccess={() => {}} />,
      ["/"],
    );

    expect(screen.getByText(/kept and marked superseded/i)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Supersede" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/reason/i), "newer evidence");
    expect(submit).toBeEnabled();
  });
});
