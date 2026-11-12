import { ApiError, client, unwrap } from "./client";

export type DocumentStatus = "ingesting" | "ready" | "failed" | "retracted";

export interface DocumentSummary {
  document_id: string;
  title: string;
  tags: string[];
  status: DocumentStatus;
  current_revision: string | null;
  created_at: string;
  created_by: string | null;
  updated_at: string;
}

export interface DocumentRevision {
  revision_id: string;
  revision_number: number;
  label: string | null;
  sha256: string;
  mime_type: string;
  filename: string | null;
  byte_size: number;
  page_count: number | null;
  extractor: string | null;
  extractor_version: string | null;
  status: "ingesting" | "ready" | "failed";
  error: string | null;
  uploaded_at: string;
  uploaded_by: string | null;
  ingested_at: string | null;
}

export interface DocumentDetail extends DocumentSummary {
  revisions: DocumentRevision[];
}

export interface UploadResult {
  document_id: string;
  revision_id: string;
  revision_number: number;
  sha256: string;
  byte_size: number;
  seq: number;
  job_id: string;
}

export interface ForgetDocumentPreview {
  document_id: string;
  title: string;
  revisions: number;
  figures: number;
  bytes: number;
  promoted_to: string | null;
  preview_token: string;
  expires_at: string;
}

export async function listDocuments(team: string): Promise<{ items: DocumentSummary[] }> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/documents", {
      params: { path: { team }, query: { limit: 500 } },
    }),
  ) as Promise<{ items: DocumentSummary[] }>;
}

export async function getDocument(team: string, documentId: string): Promise<DocumentDetail> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/documents/{document_id}", {
      params: { path: { team, document_id: documentId } },
    }),
  ) as Promise<DocumentDetail>;
}

/** The type the server accepts for a file, from its extension. */
export function documentContentType(file: File): string | null {
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".md") || name.endsWith(".markdown")) return "text/markdown";
  return null;
}

/**
 * The upload body is the file itself, which the typed client cannot send, so
 * this posts with fetch directly (same origin, session cookie).
 */
async function postFile(path: string, file: File, query: Record<string, string | undefined>) {
  const contentType = documentContentType(file);
  if (contentType === null) {
    throw new ApiError(415, "Only .pdf and .md files can be uploaded.");
  }
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...query, filename: file.name })) {
    if (v) params.set(k, v);
  }
  const response = await globalThis.fetch(`${window.location.origin}${path}?${params}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": contentType },
    body: file,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return body as UploadResult;
}

export function uploadDocument(
  team: string,
  file: File,
  opts: { title?: string; tags?: string; label?: string },
): Promise<UploadResult> {
  return postFile(`/v1/admin/teams/${encodeURIComponent(team)}/documents`, file, opts);
}

export function uploadRevision(
  team: string,
  documentId: string,
  file: File,
  opts: { label?: string },
): Promise<UploadResult> {
  return postFile(
    `/v1/admin/teams/${encodeURIComponent(team)}/documents/${encodeURIComponent(documentId)}/revisions`,
    file,
    opts,
  );
}

export async function retractDocument(team: string, documentId: string, reason?: string) {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/documents/{document_id}/retract", {
      params: { path: { team, document_id: documentId } },
      body: { reason: reason || null },
    }),
  ) as Promise<{ status: string; seq: number }>;
}

export async function previewForgetDocument(team: string, documentId: string): Promise<ForgetDocumentPreview> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/documents/{document_id}/forget/preview", {
      params: { path: { team, document_id: documentId } },
    }),
  ) as Promise<ForgetDocumentPreview>;
}

export async function forgetDocument(team: string, documentId: string, previewToken: string) {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/documents/{document_id}/forget", {
      params: { path: { team, document_id: documentId } },
      body: { preview_token: previewToken },
    }),
  ) as Promise<{ status: string; revisions: number; files_removed: number }>;
}

export async function promoteDocument(team: string, documentId: string) {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/documents/{document_id}/promote", {
      params: { path: { team, document_id: documentId } },
    }),
  ) as Promise<{ status: string; universe_document_id: string }>;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
