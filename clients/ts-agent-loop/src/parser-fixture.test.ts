/**
 * Cross-impl fixture verification (TypeScript side).
 *
 * Loads tests/fixtures/reply_prefix_cases.json (collaboratively
 * produced by personas) and asserts every case produces the same
 * (kind, expected_response, body) triple as the Python parser does.
 *
 * Run from the repo root:
 *   node --import tsx --test \
 *     clients/ts-agent-loop/src/parser-fixture.test.ts
 *
 * Drift between this and tests/test_v3_parser_cross_impl_fixture.py
 * = the cross-impl claim in protocol-v3-interop-findings.md is broken
 * and one of the parsers needs a fix (or the spec needs a clarifying
 * patch).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseReplyPrefix } from "./agent.ts";

interface FixtureCase {
  input: string;
  expected: {
    kind: string | null;
    expected_response: { from_actor_handles: string[]; kinds?: string[] } | null;
    body: string;
  };
  label?: string;
}

const FIXTURE_PATH = resolve(
  // navigate up from src/ to clients/ts-agent-loop/, then to repo root
  join(import.meta.dirname, "..", "..", "..",
       "tests", "fixtures", "reply_prefix_cases.json"),
);

function loadCases(): FixtureCase[] {
  if (!existsSync(FIXTURE_PATH)) {
    console.warn(`fixture not found: ${FIXTURE_PATH} — skipping`);
    return [];
  }
  const raw = readFileSync(FIXTURE_PATH, "utf-8");
  const cases = JSON.parse(raw) as FixtureCase[];
  if (!Array.isArray(cases)) {
    throw new Error(`fixture must be an array; got ${typeof cases}`);
  }
  return cases;
}

const cases = loadCases();

if (cases.length === 0) {
  test("fixture-not-generated", () => {
    console.warn(
      "Fixture not yet generated. Run scripts/smoke_v3_parser_fixture.sh " +
      "to populate tests/fixtures/reply_prefix_cases.json from persona collab.",
    );
  });
} else if (cases.length < 25) {
  test("fixture-has-≥25-cases", () => {
    assert.fail(`fixture has ${cases.length} cases; spec requires ≥25`);
  });
}

for (const [idx, c] of cases.entries()) {
  const label = c.label ?? `#${idx}`;
  test(`case ${label}: ${JSON.stringify(c.input).slice(0, 80)}`, () => {
    const actual = parseReplyPrefix(c.input);
    const expectedKind = c.expected.kind ?? "claim";

    assert.equal(
      actual.kind ?? "claim", expectedKind,
      `kind mismatch on input=${JSON.stringify(c.input)}: ` +
      `got=${actual.kind} expected=${expectedKind}`,
    );
    assert.equal(
      actual.body, c.expected.body,
      `body mismatch on input=${JSON.stringify(c.input)}: ` +
      `got=${JSON.stringify(actual.body)} expected=${JSON.stringify(c.expected.body)}`,
    );

    const expectedEx = c.expected.expected_response;
    if (expectedEx === null) {
      assert.equal(
        actual.expectedResponse, null,
        `expected_response should be null on input=${JSON.stringify(c.input)}, ` +
        `got ${JSON.stringify(actual.expectedResponse)}`,
      );
    } else {
      assert.notEqual(
        actual.expectedResponse, null,
        `expected_response should be set on input=${JSON.stringify(c.input)}`,
      );
      assert.deepEqual(
        actual.expectedResponse?.from_actor_handles,
        expectedEx.from_actor_handles,
        `from_actor_handles mismatch on input=${JSON.stringify(c.input)}`,
      );
      if (expectedEx.kinds !== undefined) {
        assert.deepEqual(
          actual.expectedResponse?.kinds,
          expectedEx.kinds,
          `kinds mismatch on input=${JSON.stringify(c.input)}`,
        );
      }
    }
  });
}
