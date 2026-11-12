import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { Route, Routes } from "react-router-dom";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/renderApp";
import { Documents } from "./Documents";

const DOC = {
  document_id: "d1",
  title: "Pump manual",
  tags: ["pumps"],
  status: "ready",
  current_revision: "r1",
  created_at: "2026-10-16T00:00:00Z",
  created_by: "alice",
  updated_at: "2026-10-16T00:00:00Z",
};

const DETAIL = {
  ...DOC,
  revisions: [
    {
      revision_id: "r1",
      revision_number: 1,
      label: null,
      sha256: "a".repeat(64),
      mime_type: "application/pdf",
      filename: "pump.pdf",
      byte_size: 2048,
      page_count: 12,
      extractor: "pdfplumber",
      extractor_version: "1",
      status: "ready",
      error: null,
      uploaded_at: "2026-10-16T00:00:00Z",
      uploaded_by: "alice",
      ingested_at: "2026-10-16T00:00:01Z",
    },
  ],
};

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/teams/:team/documents" element={<Documents />} />
    </Routes>,
    ["/teams/acme/documents"],
  );
}

describe("Documents", () => {
  it("lists documents and opens one with its revisions", async () => {
    server.use(
      http.get("*/v1/admin/teams/acme/documents", () => HttpResponse.json({ items: [DOC] })),
      http.get("*/v1/admin/teams/acme/documents/d1", () => HttpResponse.json(DETAIL)),
    );
    renderPage();
    await userEvent.click(await screen.findByText("Pump manual"));
    const dialog = await screen.findByRole("dialog", { name: "Pump manual" });
    expect(await within(dialog).findByText("pump.pdf")).toBeInTheDocument();
    expect(within(dialog).getByText("2.0 KB")).toBeInTheDocument();
  });

  it("uploads the file as the request body with its type and metadata", async () => {
    // jsdom's File is not a Blob to Node's fetch, so the test checks what the
    // client hands to fetch rather than the bytes MSW receives.
    server.use(
      http.get("*/v1/admin/teams/acme/documents", () => HttpResponse.json({ items: [] })),
      http.post("*/v1/admin/teams/acme/documents", () =>
        HttpResponse.json({ document_id: "d2", revision_id: "r2", revision_number: 1 }, { status: 202 }),
      ),
    );
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    const file = new File(["%PDF-1.7 hello"], "pump.pdf", { type: "application/pdf" });
    await userEvent.upload(within(dialog).getByLabelText("File (.pdf or .md)"), file);
    await userEvent.type(within(dialog).getByLabelText("Tags (comma separated)"), "pumps,fuel");
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true),
    );
    const [url, init] = fetchSpy.mock.calls.find(([, i]) => (i as RequestInit | undefined)?.method === "POST")!;
    const query = new URL(String(url)).searchParams;
    expect((init as RequestInit).body).toBe(file);
    expect(((init as RequestInit).headers as Record<string, string>)["Content-Type"]).toBe("application/pdf");
    expect(query.get("tags")).toBe("pumps,fuel");
    expect(query.get("filename")).toBe("pump.pdf");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fetchSpy.mockRestore();
  });

  it("refuses a file type the server does not accept", async () => {
    server.use(http.get("*/v1/admin/teams/acme/documents", () => HttpResponse.json({ items: [] })));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await userEvent.upload(
      within(dialog).getByLabelText("File (.pdf or .md)"),
      new File(["x"], "notes.docx"),
      { applyAccept: false },
    );
    expect(within(dialog).getByText("Only .pdf and .md files can be uploaded.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Upload" })).toBeDisabled();
  });

  it("enables Forget only once the title is typed, and sends the preview token", async () => {
    let token: string | null = null;
    server.use(
      http.get("*/v1/admin/teams/acme/documents", () => HttpResponse.json({ items: [DOC] })),
      http.get("*/v1/admin/teams/acme/documents/d1", () => HttpResponse.json(DETAIL)),
      http.post("*/v1/admin/teams/acme/documents/d1/forget/preview", () =>
        HttpResponse.json({
          document_id: "d1",
          title: "Pump manual",
          revisions: 1,
          figures: 3,
          bytes: 2048,
          promoted_to: null,
          preview_token: "tok",
          expires_at: "2026-10-16T00:05:00Z",
        }),
      ),
      http.post("*/v1/admin/teams/acme/documents/d1/forget", async ({ request }) => {
        token = ((await request.json()) as { preview_token: string }).preview_token;
        return HttpResponse.json({ status: "forgotten", revisions: 1, files_removed: 1 });
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByText("Pump manual"));
    const dialog = await screen.findByRole("dialog", { name: "Pump manual" });
    await userEvent.click(await within(dialog).findByRole("button", { name: "Forget" }));
    const input = await within(dialog).findByLabelText("Type the title to enable Forget");
    const confirm = within(dialog).getAllByRole("button", { name: "Forget" }).at(-1)!;
    expect(confirm).toBeDisabled();
    await userEvent.type(input, "Pump manual");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(token).toBe("tok"));
  });
});
