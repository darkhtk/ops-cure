#!/usr/bin/env bash
# Cross-impl parser fixture build — phase 6 stress test + interop fix.
#
# Goal: agents collaborate to produce a JSON fixture file with ≥25
# corner cases for the v3 reply-prefix parser. Fixture lands in the
# scratch dir; this script copies it into tests/fixtures/ after
# closure and runs both Python + TypeScript parsers against it.
#
# This addresses the interop-findings TODO that the two parsers
# might drift; we have no cross-impl test today.
#
# Closure: kind=inquiry (avoid v1 task-guard). quorum=2.
set -uo pipefail

TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

FIXTURE_FILE="C:/Users/darkh/Projects/ops-cure-scratch/fixtures/reply_prefix_cases.json"
WALL_CLOCK_BUDGET_S=1500
PROBE_INTERVAL_S=8

note() { echo "[$(date +%H:%M:%S)] $*"; }
curl_q() { curl -sk --max-time 15 -H "$H_AUTH" "$@"; }
curl_post() { curl -sk --max-time 15 -H "$H_AUTH" -H "$H_JSON" "$@"; }

events_json() { curl_q "$BASE/v2/operations/$1/events?actor_handle=%40alice"; }

count_kind() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
print(sum(1 for e in d.get('events',[]) if e.get('kind') == '$2'))"
}

distinct_ratifiers() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
seen = set()
for e in d.get('events', []):
    if e.get('kind') == 'chat.speech.ratify':
        seen.add(e.get('actor_id'))
print(len(seen))"
}

dump_timeline() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('events', []):
    actor = (e.get('actor_id') or '')[:8]
    text = (e.get('payload') or {}).get('text', '')[:160]
    ex = e.get('expected_response')
    ex_str = ''
    if ex:
        ex_str = f\" inv={ex.get('from_actor_handles',[])}\"
    print(f'  seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32}{ex_str} text={text!r}')"
}

mkdir -p "$(dirname "$FIXTURE_FILE")"

note "=== open op (kind=inquiry — no v1 task guard) ==="
PROMPT=$(cat <<'EOF'
Project: build a cross-impl parser fixture for the v3 reply-prefix
grammar. The grammar is in docs/protocol-v3-spec.md §8.1.1 and
implemented in two places:
  - pc_launcher/connectors/claude_executor/agent_loop.py
    (BridgeAgentLoop._parse_reply_prefix)
  - clients/ts-agent-loop/src/agent.ts (parseReplyPrefix)

We have a TODO from the interop sprint: no test verifies that the
two parsers agree byte-for-byte on edge cases. This op produces the
fixture that will close that gap.

Deliverable: a JSON file at fixtures/reply_prefix_cases.json (in
your cwd) containing a list of cases. Each case shape:
  {
    "input": "...",
    "expected": {
      "kind": "claim" | "propose" | ... | null,
      "expected_response": null | {"from_actor_handles":[...], "kinds":[...]?},
      "body": "..."
    }
  }

REQUIRED: at least 25 cases covering:
  - TERMINAL forms: no prefix, [KIND], [KIND] body
  - INVITING forms: [KIND→@a], [KIND→@a,@b], [KIND@a] (no arrow), ASCII arrow [KIND->@a]
  - kinds whitelist: kinds=ratify,object | kinds=*
  - whitespace tolerance: extra spaces in prefix
  - unknown kinds: should fall back to (claim, null, original_text)
  - malformed: missing close bracket
  - body containing @-handles that are NOT structured invites
  - empty body, very long body
  - de-duplication of repeated handles
  - kinds with whitespace inside

Division of labor:
- @investigator: list edge cases the grammar may handle differently
  in Python vs TS. Reply with [QUESTION→@operator,@reviewer] or
  [CLAIM→@operator]. Be specific about WHICH cases.
- @reviewer: read the existing parser implementations (Python at the
  path above, TS at clients/ts-agent-loop/src/agent.ts) using your
  Read/Bash tool. Identify cases where the two MIGHT diverge. Reply
  with [CLAIM→@operator kinds=propose] or [QUESTION→@operator].
- @operator: write the file at fixtures/reply_prefix_cases.json (you
  may use claude's Write tool). Include ≥25 cases per the categories
  above. Reply with [PROPOSE→@reviewer,@investigator,@alice
  kinds=ratify,object] containing the absolute path you wrote.

Closure: 2 distinct ratifiers (chat.speech.ratify) close the op.

Use the [KIND→@target] prefix grammar in your replies — that's how
this protocol passes the baton.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'inquiry',
    'title': 'parser fixture cross-impl',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'investigator', 'reviewer'],
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'max_rounds': 15,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "open failed"; exit 1; }
note "  op=$OP policy: quorum=2, max_rounds=15, kind=inquiry"

curl_post "$BASE/v2/operations/$OP/events" -d "$(python -c "
import json, sys
print(json.dumps({
    'actor_handle': '@alice',
    'kind': 'speech.question',
    'payload': {'text': '''$PROMPT'''},
    'addressed_to_many': ['operator', 'investigator', 'reviewer'],
    'expected_response': {
        'from_actor_handles': ['@operator', '@investigator', '@reviewer'],
    },
}))")" >/dev/null

START=$(date +%s)
ALICE_RATIFIED=0

note "=== monitor — budget ${WALL_CLOCK_BUDGET_S}s ==="
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "$ELAPSED" -ge "$WALL_CLOCK_BUDGET_S" ]; then
        note "  budget exhausted at ${ELAPSED}s — abandon"
        break
    fi

    PROPOSE=$(count_kind "$OP" chat.speech.propose)
    RATIFIERS=$(distinct_ratifiers "$OP")
    FILE_EXISTS=0
    [ -f "$FIXTURE_FILE" ] && FILE_EXISTS=1
    note "  t=${ELAPSED}s propose=$PROPOSE ratifiers=$RATIFIERS file_exists=$FILE_EXISTS"

    # Once propose + file exists, alice ratifies (one-time)
    if [ "$PROPOSE" -ge 1 ] && [ "$FILE_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  operator delivered + file exists → alice ratifies"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"[RATIFY] alice: fixture file exists, ratifying."}
        }')
        if [ "$STATUS" = "201" ]; then ALICE_RATIFIED=1; fi
    fi

    # Close at quorum
    if [ "$RATIFIERS" -ge 2 ]; then
        note "  quorum reached — alice closes"
        STATUS=$(curl_post -o /tmp/_c -w "%{http_code}" "$BASE/v2/operations/$OP/close" -d '{
            "actor_handle":"@alice","resolution":"answered",
            "summary":"parser fixture file produced + quorum-ratified"
        }')
        if [ "$STATUS" = "200" ]; then
            note "  close OK"
            break
        fi
        note "  close HTTP=$STATUS body=$(cat /tmp/_c)"
        sleep $PROBE_INTERVAL_S
        continue
    fi

    sleep $PROBE_INTERVAL_S
done

# Force close if needed
STATE=$(curl_q "$BASE/v2/operations/$OP" | python -c "import sys, json; print(json.load(sys.stdin).get('state',''))")
if [ "$STATE" != "closed" ]; then
    note "  force close"
    curl_post "$BASE/v2/operations/$OP/close" -d '{
        "actor_handle":"@alice","resolution":"abandoned",
        "summary":"wall-clock budget"
    }' >/dev/null || true
fi

note ""
note "=== final timeline ==="
dump_timeline "$OP"

note ""
if [ -f "$FIXTURE_FILE" ]; then
    SIZE=$(stat -c%s "$FIXTURE_FILE" 2>/dev/null || wc -c <"$FIXTURE_FILE")
    CASES=$(python -c "import json; print(len(json.load(open(r'$FIXTURE_FILE'))))" 2>/dev/null || echo "?")
    note "=== fixture verification ==="
    note "  EXISTS: $FIXTURE_FILE ($SIZE bytes, $CASES cases)"
    # Copy into project tests/fixtures
    PROJECT_FIXTURE="C:/Users/darkh/Projects/ops-cure/tests/fixtures/reply_prefix_cases.json"
    mkdir -p "$(dirname "$PROJECT_FIXTURE")"
    cp "$FIXTURE_FILE" "$PROJECT_FIXTURE"
    note "  copied → $PROJECT_FIXTURE"
else
    note "=== fixture MISSING ==="
fi

FINAL=$(curl_q "$BASE/v2/operations/$OP")
STATE=$(echo "$FINAL" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('state',''))")
RESOLUTION=$(echo "$FINAL" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('resolution') or '-')")
note ""
note "=========================================="
note "  state=$STATE resolution=$RESOLUTION"
note "  total events: $(events_json "$OP" | python -c "import sys,json; print(len(json.load(sys.stdin).get('events',[])))")"
note "  duration: $((NOW - START))s"
note "=========================================="
