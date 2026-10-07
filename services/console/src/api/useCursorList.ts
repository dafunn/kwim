import { useInfiniteQuery } from "@tanstack/react-query";
import type { ListEnvelope } from "./pagination";

/**
 * A "Load more" walk over a cursor-paginated list endpoint (no client-side
 * load-all - one page fetched per click). Stops on a null `next_cursor`,
 * never on an empty `items` page, by handing next_cursor straight to
 * react-query's getNextPageParam.
 */
export function useCursorList<T>(
  queryKey: readonly unknown[],
  fetchPage: (cursor: string | null) => Promise<ListEnvelope<T>>,
  options?: { enabled?: boolean },
) {
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: options?.enabled,
  });

  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const total = query.data?.pages.at(-1)?.total ?? null;

  return { ...query, items, total };
}
