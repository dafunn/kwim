import { client, unwrap } from "./client";

export interface Operator {
  id: string;
  username: string;
  display_name: string | null;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export async function listOperators(): Promise<{ items: Operator[] }> {
  return unwrap(client.GET("/v1/admin/operators")) as Promise<{ items: Operator[] }>;
}
