import { client, unwrap } from "./client";

export type SkillStatus = "current" | "retired";

export interface PendingRevision {
  proposal_id: string;
  proposed_at: string;
  proposed_by: string | null;
  summary: string;
}

export interface SkillSummary {
  id: string;
  name: string;
  status: SkillStatus;
  version: number;
  description: string;
  agents: string[];
  tags: string[];
  refs: string[];
  owner: string | null;
  promoted_to: string | null;
  promoted_from_team: string | null;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  pending_revision: PendingRevision | null;
}

export interface SkillVersion {
  version: number;
  description: string;
  source: "human" | "agent_proposal" | "promotion";
  author: string | null;
  approver: string | null;
  proposal_id: string | null;
  created_at: string;
  text?: string;
}

export interface SkillDetail extends SkillSummary {
  text: string;
  versions: SkillVersion[];
}

export interface PromoteResult {
  status: "promoted" | "pending_review";
  name?: string;
  universe_version?: number;
  proposal_id?: string;
  detail?: string;
}

export async function listSkills(team: string): Promise<{ items: SkillSummary[] }> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/skills", {
      params: { path: { team }, query: { include_retired: true } },
    }),
  ) as Promise<{ items: SkillSummary[] }>;
}

export async function getSkill(team: string, name: string): Promise<SkillDetail> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/skills/{name}", { params: { path: { team, name } } }),
  ) as Promise<SkillDetail>;
}

export async function getSkillVersion(team: string, name: string, version: number): Promise<SkillVersion> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/skills/{name}/versions/{version}", {
      params: { path: { team, name, version } },
    }),
  ) as Promise<SkillVersion>;
}

export async function createSkill(team: string, text: string, owner?: string): Promise<SkillSummary> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/skills", {
      params: { path: { team } },
      body: { text, owner: owner || null },
    }),
  ) as Promise<SkillSummary>;
}

export async function reviseSkill(team: string, name: string, text: string): Promise<SkillSummary> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/skills/{name}/revisions", {
      params: { path: { team, name } },
      body: { text },
    }),
  ) as Promise<SkillSummary>;
}

export async function retireSkill(team: string, name: string): Promise<unknown> {
  return unwrap(client.POST("/v1/admin/teams/{team}/skills/{name}/retire", { params: { path: { team, name } } }));
}

export async function restoreSkill(team: string, name: string): Promise<unknown> {
  return unwrap(client.POST("/v1/admin/teams/{team}/skills/{name}/restore", { params: { path: { team, name } } }));
}

export async function setSkillOwner(team: string, name: string, owner: string): Promise<unknown> {
  return unwrap(
    client.PUT("/v1/admin/teams/{team}/skills/{name}/owner", {
      params: { path: { team, name } },
      body: { owner },
    }),
  );
}

export async function promoteSkill(team: string, name: string): Promise<PromoteResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/skills/{name}/promote", { params: { path: { team, name } } }),
  ) as Promise<PromoteResult>;
}
