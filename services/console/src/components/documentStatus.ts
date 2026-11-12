export function statusTone(status: string): "good" | "bad" | "warn" | "neutral" {
  if (status === "ready") return "good";
  if (status === "failed") return "bad";
  if (status === "ingesting") return "warn";
  return "neutral";
}
