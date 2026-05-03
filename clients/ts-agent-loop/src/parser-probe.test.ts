/**
 * Edge-case probes for cross-impl drift discovery. NOT in the
 * conformance fixture — these are exploratory inputs that I
 * specifically suspected might diverge after staring at both source
 * trees. The fixture file (reply_prefix_cases.json) covers the
 * happy path + documented corners; this file probes the boundaries.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { parseReplyPrefix } from "./agent.ts";

test("PROBE-A — `]` at exact index 200", () => {
  // Input: '[CLAIM' + 194 spaces + ']' + 'body'.
  // ']' lands at index 200. Python's str.find("]", 1, 200) treats
  // 200 as exclusive — rejects. TS' indexOf("]", 1) returns 200,
  // and the bound check is `end > 200` — accepts.
  const inside = "CLAIM" + " ".repeat(194);
  const probe = "[" + inside + "]" + "body";
  assert.equal(probe.length, 205);
  assert.equal(probe[200], "]");
  const r = parseReplyPrefix(probe);
  console.log(JSON.stringify({
    case: "]-at-200",
    kind: r.kind, ex: r.expectedResponse, body: r.body.slice(0, 40),
  }));
});

test("PROBE-B — NBSP inside prefix (U+00A0)", () => {
  const nbsp = " ";
  const probe = `[PROPOSE${nbsp}->${nbsp}@a,${nbsp}@b]${nbsp}body`;
  const r = parseReplyPrefix(probe);
  console.log(JSON.stringify({
    case: "nbsp-in-prefix",
    kind: r.kind, ex: r.expectedResponse, body: r.body,
  }));
});

test("PROBE-C — arrow with no handles", () => {
  const r = parseReplyPrefix("[PROPOSE->] body");
  console.log(JSON.stringify({
    case: "arrow-empty",
    kind: r.kind, ex: r.expectedResponse, body: r.body,
  }));
});

test("PROBE-D — kinds= with empty value", () => {
  const r = parseReplyPrefix("[PROPOSE->@a kinds=] body");
  console.log(JSON.stringify({
    case: "kinds-empty",
    kind: r.kind, ex: r.expectedResponse, body: r.body,
  }));
});
