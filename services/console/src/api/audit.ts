import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";

/** Every mutation writes exactly one of these (admin_write.py's `audited`). */
export interface AuditLogRow {
  seq: number;
  at: string;
  operator_id: string | null;
  action: string;
  team: string | null;
  object_type: string | null;
  object_id: string | null;
  detail: Record<string, unknown>;
  result: "ok" | "denied" | "error";
}

export interface AuditFilters {
  team?: string;
  operator?: string;
  action?: string;
  since?: string;
}

export async function listAudit(
  filters: AuditFilters,
  cursor: string | null,
  limit = 100,
): Promise<ListEnvelope<AuditLogRow>> {
  return unwrap(
    client.GET("/v1/admin/audit", {
      params: { query: { ...filters, cursor: cursor ?? undefined, limit } },
    }),
  ) as Promise<ListEnvelope<AuditLogRow>>;
}
