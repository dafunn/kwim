import { Badge } from "./Badge";

interface FreshnessProps {
  band: string;
  asOf: string;
}

const TONE: Record<string, "good" | "warn" | "bad"> = {
  fresh: "good",
  aging: "warn",
  stale: "bad",
};

/** The computed band, with `as_of` available on hover. */
export function Freshness({ band, asOf }: FreshnessProps) {
  const title = asOf ? `as of ${new Date(asOf).toLocaleString()}` : undefined;
  return (
    <Badge tone={TONE[band] ?? "neutral"} title={title}>
      {band}
    </Badge>
  );
}
