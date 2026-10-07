import { client, unwrap } from "./client";

export interface RetractResult {
  seq: number;
}

export interface ConfirmResult {
  seq: number;
}

export interface ForgetPlanItem {
  id: string;
  type: string;
  status: string;
  label: string | null;
  evidence: string[];
  episodics_to_delete: string[];
  episodics_shared: { episodic: string; also_supports: string[] }[];
}

export interface ForgetPreflight {
  role: string | null;
  can_delete: boolean;
}

export interface ForgetPreviewResponse {
  preview_token: string;
  expires_at: string;
  count: number;
  plan: ForgetPlanItem[];
  preflight: ForgetPreflight;
}

export interface ForgetExecuteResponse {
  forgotten: number;
  commit_log_rows: number;
  episodic_events: number;
  shared_skipped: { episodic: string; also_supports: string[] }[];
}

export async function retractObject(
  team: string,
  objectId: string,
  body: { object_type: string; reason: string },
): Promise<RetractResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/objects/{object_id}/retract", {
      params: { path: { team, object_id: objectId } },
      body,
    }),
  ) as Promise<RetractResult>;
}

export async function confirmObject(
  team: string,
  objectId: string,
  body: { object_type: string },
): Promise<ConfirmResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/objects/{object_id}/confirm", {
      params: { path: { team, object_id: objectId } },
      body,
    }),
  ) as Promise<ConfirmResult>;
}

/**
 * The token carries the selection, not the resolved plan: a 409 on execute
 * means the set changed since this preview - re-run this and
 * show the operator the new plan, never retry execute with the same token.
 */
export async function previewForget(
  team: string,
  body: { object_ids?: string[]; select?: Record<string, unknown> },
): Promise<ForgetPreviewResponse> {
  return unwrap(
    // openapi-typescript can't express ForgetPreviewRequest.select (dict[str, Any]).
    client.POST("/v1/admin/teams/{team}/forget/preview", { params: { path: { team } }, body: body as never }),
  ) as Promise<ForgetPreviewResponse>;
}

export async function executeForget(
  team: string,
  body: { preview_token: string; confirm_count: number },
): Promise<ForgetExecuteResponse> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/forget", { params: { path: { team } }, body }),
  ) as Promise<ForgetExecuteResponse>;
}
