import { client, unwrap } from "./client";

export interface CodeFunction {
  id: string;
  name: string;
  signature: string;
  summary: string;
  repo: string;
  path: string;
  score?: number | null;
  path_confidence?: number | null;
}

export interface CodeArchitecture {
  communities: Record<string, unknown>[];
}

export interface CodeChange {
  repo: string;
  path: string;
  commit: string;
}

export async function listRepos(team: string): Promise<string[]> {
  return unwrap(client.GET("/v1/admin/teams/{team}/code/repos", { params: { path: { team } } })) as Promise<
    string[]
  >;
}

export async function searchCode(
  team: string,
  opts: { q?: string; name?: string; repos?: string[]; limit?: number } = {},
): Promise<CodeFunction[]> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/code/search", { params: { path: { team }, query: opts } }),
  );
}

export async function traceCode(
  team: string,
  fnId: string,
  opts: { direction?: "outbound" | "inbound"; depth?: number; min_confidence?: number } = {},
): Promise<CodeFunction[]> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/code/functions/{fn_id}/trace", {
      params: { path: { team, fn_id: fnId }, query: opts },
    }),
  );
}

export async function getArchitecture(team: string, repos?: string[]): Promise<CodeArchitecture> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/code/architecture", { params: { path: { team }, query: { repos } } }),
  ) as Promise<CodeArchitecture>;
}

export async function listChanges(team: string, repo: string, commit: string): Promise<CodeChange[]> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/code/changes", { params: { path: { team }, query: { repo, commit } } }),
  );
}
