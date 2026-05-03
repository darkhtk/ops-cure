/**
 * W3C traceparent helpers. Spec §5.
 *
 * Format: `00-<32 hex trace_id>-<16 hex span_id>-<2 hex flags>`
 *
 * The bridge mints fresh span_ids per request but preserves trace_id.
 * Our client captures the SSE session's trace_id and reuses it on
 * subsequent POSTs so the bridge can correlate "event delivered to
 * agent" with "agent's reply" under one trace.
 */

const PATTERN = /^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

export interface TraceparentParts {
  traceId: string;
  spanId: string;
  flags: string;
}

export function parseTraceparent(value: string | undefined | null): TraceparentParts | null {
  if (!value) return null;
  const m = PATTERN.exec(value.trim().toLowerCase());
  if (!m) return null;
  // m[1..3] guaranteed non-undefined when match succeeds, but TS strict
  // doesn't know that; assert with `as string`.
  return {
    traceId: m[1] as string,
    spanId: m[2] as string,
    flags: m[3] as string,
  };
}

function randomHex(byteLength: number): string {
  // Node 24 has globalThis.crypto. Reach for it explicitly so tooling
  // reports a clear error if a bundler stripped the global.
  const buf = new Uint8Array(byteLength);
  globalThis.crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function newTraceparent(): string {
  return `00-${randomHex(16)}-${randomHex(8)}-01`;
}
