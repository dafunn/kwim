import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { Route, Routes } from "react-router-dom";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/renderApp";
import { lineDiff } from "../../components/lineDiff";
import { Skills } from "./Skills";

const V1 = "---\nname: check-host-cpu\ndescription: CPU on a host.\n---\n\nRun the playbook.";
const V2 = "---\nname: check-host-cpu\ndescription: CPU on a host.\n---\n\nRun the report playbook.";

const SKILL = {
  id: "s1",
  name: "check-host-cpu",
  status: "current",
  version: 2,
  description: "CPU on a host.",
  agents: ["ops"],
  tags: ["host"],
  refs: ["org/repo:playbooks/report-host-cpu.yml"],
  owner: "alice",
  promoted_to: null,
  promoted_from_team: null,
  created_at: "2026-10-19T00:00:00Z",
  created_by: "alice",
  updated_at: "2026-10-19T00:00:00Z",
  pending_revision: {
    proposal_id: "p1",
    proposed_at: "2026-10-19T01:00:00Z",
    proposed_by: "devkey",
    summary: "CPU on a host.",
  },
};

const VERSIONS = [
  { version: 2, description: "CPU on a host.", source: "human", author: "alice", approver: "alice", proposal_id: null, created_at: "2026-10-19T00:00:00Z" },
  { version: 1, description: "CPU on a host.", source: "human", author: "alice", approver: "alice", proposal_id: null, created_at: "2026-10-18T00:00:00Z" },
];

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/teams/:team/skills" element={<Skills />} />
    </Routes>,
    ["/teams/acme/skills"],
  );
}

describe("Skills", () => {
  it("lists skills, opens one, and shows a version's changes", async () => {
    server.use(
      http.get("*/v1/admin/teams/acme/skills", () =>
        HttpResponse.json({ items: [SKILL, { ...SKILL, id: "s2", name: "orphan", owner: null, pending_revision: null }] }),
      ),
      http.get("*/v1/admin/teams/acme/skills/check-host-cpu", () =>
        HttpResponse.json({ ...SKILL, text: V2, versions: VERSIONS }),
      ),
      http.get("*/v1/admin/teams/acme/skills/check-host-cpu/versions/1", () =>
        HttpResponse.json({ ...VERSIONS[1], name: "check-host-cpu", text: V1 }),
      ),
      http.get("*/v1/admin/teams/acme/skills/check-host-cpu/versions/2", () =>
        HttpResponse.json({ ...VERSIONS[0], name: "check-host-cpu", text: V2 }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/have no owner: orphan/)).toBeInTheDocument();
    await userEvent.click(await screen.findByText("check-host-cpu"));
    const dialog = await screen.findByRole("dialog", { name: "check-host-cpu" });
    expect(await within(dialog).findByText(/waiting for review/)).toBeInTheDocument();
    expect(within(dialog).getByText("org/repo:playbooks/report-host-cpu.yml")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Changes" }));
    const changes = await within(dialog).findByLabelText("changes in version 2");
    expect(changes).toHaveTextContent("- Run the playbook.");
    expect(changes).toHaveTextContent("+ Run the report playbook.");
  });

  it("creates a skill from its SKILL.md text", async () => {
    let posted: unknown = null;
    server.use(
      http.get("*/v1/admin/teams/acme/skills", () => HttpResponse.json({ items: [] })),
      http.post("*/v1/admin/teams/acme/skills", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ...SKILL, version: 1 }, { status: 201 });
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "New skill" }));
    const dialog = await screen.findByRole("dialog", { name: "New skill" });
    const textarea = within(dialog).getByLabelText("SKILL.md");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "text of the skill");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create skill" }));
    await screen.findByText("No skills.");
    expect(posted).toEqual({ text: "text of the skill", owner: null });
  });
});

describe("lineDiff", () => {
  it("marks removed and added lines", () => {
    expect(lineDiff("a\nb\nc", "a\nx\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "added", text: "x" },
      { kind: "same", text: "c" },
    ]);
  });
});
