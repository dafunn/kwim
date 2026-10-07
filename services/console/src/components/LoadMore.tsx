import styles from "./ui.module.css";

interface LoadMoreProps {
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onClick: () => void;
}

/** One page per click, never a client-side load-all. */
export function LoadMore({ hasNextPage, isFetchingNextPage, onClick }: LoadMoreProps) {
  if (!hasNextPage) return null;
  return (
    <button type="button" className={styles.loadMore} onClick={onClick} disabled={isFetchingNextPage}>
      {isFetchingNextPage ? "Loading..." : "Load more"}
    </button>
  );
}
