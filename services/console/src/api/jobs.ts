import { client, unwrap } from "./client";

export interface Job {
  job_id: string;
  kind: string;
  team: string;
  status: "running" | "succeeded" | "failed";
  started_at: string | null;
  finished_at: string | null;
  detail: Record<string, unknown>;
}

/** No cursor/limit on this route (admin_write.py) - AdminStore.list_jobs caps at 50 server-side. */
export async function listJobs(filters: { team?: string; status?: string } = {}): Promise<{ items: Job[] }> {
  return unwrap(client.GET("/v1/admin/jobs", { params: { query: filters } })) as Promise<{ items: Job[] }>;
}

export async function getJob(jobId: string): Promise<Job> {
  return unwrap(client.GET("/v1/admin/jobs/{job_id}", { params: { path: { job_id: jobId } } })) as Promise<Job>;
}
