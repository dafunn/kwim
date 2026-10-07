#!/usr/bin/env node
// Build-time check: the bundle must not reference localStorage or
// sessionStorage outside this allowlist.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const DIST = fileURLToPath(new URL("../dist", import.meta.url));
const ALLOWLIST = new Set([
  // e.g. "assets/index-abc123.js" - none today.
]);
const PATTERN = /\b(localStorage|sessionStorage)\b/;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith(".js")) out.push(full);
  }
  return out;
}

const offenders = [];
for (const file of walk(DIST)) {
  const rel = file.slice(DIST.length + 1);
  if (ALLOWLIST.has(rel)) continue;
  if (PATTERN.test(readFileSync(file, "utf8"))) offenders.push(rel);
}

if (offenders.length > 0) {
  console.error("U4 violation: localStorage/sessionStorage found in built bundle:");
  for (const f of offenders) console.error(`  ${f}`);
  process.exit(1);
}
console.log("storage check passed: no localStorage/sessionStorage in bundle");
