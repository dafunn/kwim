import { client, unwrap } from "./client";

/** GET /health has no response_model in the service; shape verified against main.py. */
export interface HealthStatus {
  status: string;
  service: string;
  version: string;
}

export async function getHealth(): Promise<HealthStatus> {
  return unwrap(client.GET("/health")) as Promise<HealthStatus>;
}
