import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";
import type { AmendResult } from "./facts";

export interface SemanticItem {
  id: string;
  content: string;
  metadata: Record<string, unknown>;
  /** Cosine distance, search mode only - 0.0 for a plain list (no query). */
  score: number;
}

/**
 * Listing (no `q`) is the capability the team-key API can't do at all; `q`
 * switches to a vector search, which the server never paginates
 * (next_cursor is always null in that mode).
 */
export async function listSemantic(
  team: string,
  opts: { q?: string; cursor?: string | null; limit?: number } = {},
): Promise<ListEnvelope<SemanticItem>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/semantic", {
      params: {
        path: { team },
        query: { q: opts.q, cursor: opts.cursor ?? undefined, limit: opts.limit ?? 50 },
      },
    }),
  ) as Promise<ListEnvelope<SemanticItem>>;
}

export interface AmendSemanticBody {
  content?: string;
  metadata?: Record<string, unknown>;
  reason: string;
}

/** Semantic items have amend only, having no version chain. */
export async function amendSemantic(
  team: string,
  itemId: string,
  body: AmendSemanticBody,
): Promise<AmendResult> {
  return unwrap(
    client.PATCH("/v1/admin/teams/{team}/semantic/{item_id}/amend", {
      params: { path: { team, item_id: itemId } },
      // openapi-typescript can't express AmendSemanticRequest.metadata (dict[str, Any]).
      body: body as never,
    }),
  ) as Promise<AmendResult>;
}

export interface CreateSemanticBody {
  id?: string;
  content: string;
  metadata?: Record<string, unknown>;
}

export async function createSemantic(team: string, body: CreateSemanticBody): Promise<{ id: string; seq: number }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/semantic", { params: { path: { team } }, body: body as never }),
  ) as Promise<{ id: string; seq: number }>;
}
