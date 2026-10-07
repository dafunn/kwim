import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import { redirectToLogin } from "../auth/session";

function shouldRetry(failureCount: number, error: unknown): boolean {
  // These statuses are answers, not transient failures; never retried.
  if (error instanceof ApiError && [401, 403, 404, 409, 422, 429].includes(error.status)) {
    return false;
  }
  return failureCount < 2;
}

/**
 * A factory, so each test gets its own client; the app uses one (below).
 */
export function createQueryClient(): QueryClient {
  let queryClient: QueryClient;

  // A 401 from any call clears local state and returns to login.
  function handleError(error: unknown) {
    if (error instanceof ApiError && error.status === 401) {
      queryClient.clear();
      redirectToLogin();
    }
  }

  queryClient = new QueryClient({
    queryCache: new QueryCache({ onError: handleError }),
    mutationCache: new MutationCache({ onError: handleError }),
    defaultOptions: {
      queries: { retry: shouldRetry },
      mutations: { retry: false },
    },
  });

  return queryClient;
}

export const queryClient = createQueryClient();
