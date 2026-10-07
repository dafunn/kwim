import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";
import type { CommitLogRow } from "./commitLog";
import type { AmendResult, SupersedeResult } from "./facts";

export interface Rule {
  id: string;
  rule_type: string;
  situation: Record<string, unknown> | null;
  approach: string | null;
  evidence_count: number;
  status: string; // "pending" | "approved" | "deprecated" | "retracted"
  scope: string | null;
  action_pattern: string | null;
  verdict: string | null;
  authority: string | null;
  severity: string | null;
  check_tier: string | null;
}

export interface RuleProvenance {
  proposed_by: string | null;
  learned_from: string | null;
  promoted_from_id: string | null;
  promoted_from_team: string | null;
}

export interface RuleDetail {
  rule: Rule;
  provenance: RuleProvenance;
  commit_rows: CommitLogRow[];
}

export interface RuleFilters {
  status?: string;
  rule_type?: string;
  scope?: string;
}

export async function listRules(
  team: string,
  filters: RuleFilters,
  cursor: string | null,
  limit = 50,
): Promise<ListEnvelope<Rule>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/rules", {
      params: {
        path: { team },
        query: { ...filters, cursor: cursor ?? undefined, limit },
      },
    }),
  ) as Promise<ListEnvelope<Rule>>;
}

export async function getRule(team: string, ruleId: string): Promise<RuleDetail> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/rules/{rule_id}", {
      params: { path: { team, rule_id: ruleId } },
    }),
  ) as Promise<RuleDetail>;
}

export interface AmendRuleBody {
  situation?: Record<string, unknown>;
  approach?: string;
  action_pattern?: string;
  verdict?: string;
  authority?: string;
  severity?: string;
  check_tier?: string;
  reason: string;
}

export async function amendRule(team: string, ruleId: string, body: AmendRuleBody): Promise<AmendResult> {
  return unwrap(
    client.PATCH("/v1/admin/teams/{team}/rules/{rule_id}/amend", {
      params: { path: { team, rule_id: ruleId } },
      // openapi-typescript can't express AmendRuleRequest.situation (dict[str, Any]);
      // the runtime contract is AmendRuleRequest in admin_write.py, verified by hand.
      body: body as never,
    }),
  ) as Promise<AmendResult>;
}

export interface SupersedeRuleBody {
  rule_type?: string;
  situation?: Record<string, unknown>;
  approach: string;
  evidence?: string[];
  reason: string;
}

export async function supersedeRule(
  team: string,
  ruleId: string,
  body: SupersedeRuleBody,
): Promise<SupersedeResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/rules/{rule_id}/supersede", {
      params: { path: { team, rule_id: ruleId } },
      // Same codegen limitation as amendRule above (situation: dict[str, Any]).
      body: body as never,
    }),
  ) as Promise<SupersedeResult>;
}

export interface CreateAdvisoryRuleBody {
  rule_type: "advisory";
  situation?: Record<string, unknown>;
  approach: string;
  evidence?: string[];
  reinforces?: string;
  source_kind?: "agent_proposal" | "repo_sync" | "distiller";
}

export interface CreateConstraintRuleBody {
  rule_type: "constraint";
  action_pattern: string;
  verdict: "allow" | "deny" | "escalate";
  authority: string;
  severity: string;
  check_tier: "deterministic" | "classifier";
  source_kind?: "agent_proposal" | "repo_sync" | "distiller";
}

export type CreateRuleResult =
  | { object_id: string; seq: number }
  | { proposal_id: string; status: "pending_review" };

/**
 * Advisory rules commit directly; constraint rules always route to review
 * (admin_write.py: enforcement policy is too consequential to auto-commit).
 */
export async function createRule(
  team: string,
  body: CreateAdvisoryRuleBody | CreateConstraintRuleBody,
): Promise<CreateRuleResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/rules", { params: { path: { team } }, body: body as never }),
  ) as Promise<CreateRuleResult>;
}
