import { client, unwrap } from "./client";

/** Diagnostic only - keys and TTLs, never values; working memory is ephemeral and not rebuilt. */
export interface WorkingKey {
  session: string;
  key: string;
  ttl_seconds: number | null;
}

export async function listWorking(team: string, session: string): Promise<{ items: WorkingKey[] }> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/working", {
      params: { path: { team }, query: { session } },
    }),
  ) as Promise<{ items: WorkingKey[] }>;
}
