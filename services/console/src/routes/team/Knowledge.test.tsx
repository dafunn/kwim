import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/renderApp";
import { listFacts, type Fact } from "../../api/facts";
import { useCursorList } from "../../api/useCursorList";
import { LoadMore } from "../../components/LoadMore";

function makeFact(i: number): Fact {
  return {
    id: `fact-${i}`,
    statement: `statement ${i}`,
    fact_type: "note",
    status: "current",
    created_at: "2026-01-01T00:00:00Z",
    about: [],
    decay_class: "slow",
    as_of: "2026-01-01T00:00:00Z",
    freshness: "fresh",
    last_verified_at: null,
    source_kind: null,
  };
}

/**
 * The mock encodes an offset in the cursor it returns and honours it, so the
 * test fails if the client ignores next_cursor.
 */
function installFactsHandler(total: number) {
  const rows = Array.from({ length: total }, (_, i) => makeFact(i));
  let requestCount = 0;

  server.use(
    http.get("*/v1/admin/teams/:team/facts", ({ request }) => {
      requestCount += 1;
      const url = new URL(request.url);
      const cursor = url.searchParams.get("cursor");
      const limit = Number(url.searchParams.get("limit"));
      const offset = cursor ? Number(cursor) : 0;
      const page = rows.slice(offset, offset + limit);
      const next_cursor = page.length === limit ? String(offset + limit) : null;
      return HttpResponse.json({ items: page, next_cursor, total: null });
    }),
  );

  return () => requestCount;
}

/**
 * listFacts() and useCursorList() through MSW: one page per Load more click.
 */
function FactsPager({ team }: { team: string }) {
  const { items, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<Fact>(
    ["test", "facts", team],
    (cursor) => listFacts(team, {}, cursor, 10),
  );
  return (
    <div>
      <ul>
        {items.map((f) => (
          <li key={f.id}>{f.statement}</li>
        ))}
      </ul>
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
    </div>
  );
}

describe("cursor pagination", () => {
  it("walks a 25-row fixture at 10/page in exactly 3 requests, stopping on a null next_cursor", async () => {
    const getRequestCount = installFactsHandler(25);
    const user = userEvent.setup();

    renderWithProviders(<FactsPager team="acme" />, ["/"]);

    // Page 1: 10 rows, one request so far.
    await waitFor(() => expect(screen.getByText("statement 9")).toBeInTheDocument());
    expect(screen.queryByText("statement 10")).not.toBeInTheDocument();
    expect(getRequestCount()).toBe(1);

    // Page 2.
    await user.click(screen.getByRole("button", { name: /load more/i }));
    await waitFor(() => expect(screen.getByText("statement 19")).toBeInTheDocument());
    expect(getRequestCount()).toBe(2);

    // Page 3 is short and returns no cursor, so Load more disappears.
    await user.click(screen.getByRole("button", { name: /load more/i }));
    await waitFor(() => expect(screen.getByText("statement 24")).toBeInTheDocument());
    expect(getRequestCount()).toBe(3);
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(25);
  });
});
