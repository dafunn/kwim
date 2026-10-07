import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";

export interface EpisodicEvent {
  id: string;
  agent_id: string;
  session_id: string;
  event_type: string;
  event_data: Record<string, unknown>;
  occurred_at: string;
  archived: boolean;
}

export interface EpisodicFilters {
  event_type?: string;
  agent_id?: string;
  order?: "asc" | "desc";
  archived?: "false" | "true" | "any";
}

export async function listEpisodic(
  team: string,
  filters: EpisodicFilters,
  cursor: string | null,
  limit = 100,
): Promise<ListEnvelope<EpisodicEvent>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/episodic", {
      params: {
        path: { team },
        query: { ...filters, cursor: cursor ?? undefined, limit },
      },
    }),
  ) as Promise<ListEnvelope<EpisodicEvent>>;
}
