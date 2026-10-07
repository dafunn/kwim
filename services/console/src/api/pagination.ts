/** The list envelope every /v1/admin list endpoint returns. */
export interface ListEnvelope<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}
