#!/usr/bin/env bash
# Closure-mechanism stress test: a real single-decision project run
# entirely by 3 personas, with multiple safety nets to GUARANTEE the
# op terminates regardless of LLM behavior.
#
# Project: "Read protocol-v3-spec.md from an external implementer's
# POV. Propose the 3 biggest ambiguities + a boundary scenario each."
#
# Closure safety nets (any one of them MUST end this op):
#   1. policy.max_rounds=20 — bridge rejects 21st speech
#   2. policy.close_policy=quorum + min_ratifiers=2 — needs 2 distinct
#      ratifiers before close is admissible
#   3. policy.by_round_seq sweeper hint — auto-DEFER on stuck reply
#   4. Hard wall-clock budget in this script (alice closes at deadline
#      regardless of consensus state)
#
# Expected outcome:
#   PASS: state=closed within budget, resolution recorded, transcript
#         contains operator [PROPOSE] + ≥2 ratifiers OR alice's
#         budget-driven close.
set -uo pipefail

TID="${1:?need discord_thread_id (run scripts/nas-mkthread.ps1)}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

WALL_CLOCK_BUDGET_S=900   # 15 minutes hard ceiling
PROBE_INTERVAL_S=5        # poll cadence
PROPOSE_NUDGE_AT_S=180    # if no propose by 3min, nudge operator

note() { echo "[$(date +%H:%M:%S)] $*"; }

curl_q() { curl -sk --max-time 15 -H "$H_AUTH" "$@"; }
curl_post() { curl -sk --max-time 15 -H "$H_AUTH" -H "$H_JSON" "$@"; }

events_json() {
    curl_q "$BASE/v2/operations/$1/events?actor_handle=%40alice"
}

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
    text = (e.get('payload') or {}).get('text', '')[:140]
    print(f'  seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32} text={text!r}')"
}

note "=== open op ==="
PROMPT=$(cat <<'EOF'
Project: read docs/protocol-v3-spec.md from an OUTSIDE implementer's
point of view. Identify the 3 BIGGEST ambiguities a third-party
implementer would hit, and for each, name one concrete boundary
scenario where it bites.

Ground rules:
- One round of clarifying probes from each persona is enough.
- @operator MUST then post a single chat.speech.propose with the
  final 3 items, formatted as a numbered list.
- After @operator's propose, anyone with an opinion must post
  chat.speech.ratify (concur) or chat.speech.object (push back).
- Quorum (2 distinct ratifiers) closes the op.

Persona roles: @investigator probes / questions, @reviewer critiques,
@operator drives toward proposal.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'inquiry',
    'title': 'spec ambiguity audit',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['investigator', 'reviewer', 'operator'],
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'max_rounds': 20,
    },
}))")" | python -c "import sys, json; print(json.load(sys.stdin)['id'])")
note "  op_id=$OP policy: quorum=2, max_rounds=20"

curl_post "$BASE/v2/operations/$OP/events" -d "$(python -c "
import json, sys
prompt = '''$PROMPT'''
print(json.dumps({
    'actor_handle': '@alice',
    'kind': 'speech.question',
    'payload': {'text': prompt},
    'addressed_to_many': ['investigator', 'reviewer', 'operator'],
    'expected_response': {
        'from_actor_handles': ['@investigator', '@reviewer', '@operator'],
        'by_round_seq': 18,
    },
}))")" >/dev/null

START=$(date +%s)
NUDGED=0

note "=== monitor — budget ${WALL_CLOCK_BUDGET_S}s ==="
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "$ELAPSED" -ge "$WALL_CLOCK_BUDGET_S" ]; then
        note "  wall-clock budget exhausted at ${ELAPSED}s — alice forces close"
        break
    fi

    PROPOSE=$(count_kind "$OP" chat.speech.propose)
    RATIFIERS=$(distinct_ratifiers "$OP")
    note "  t=${ELAPSED}s propose=$PROPOSE ratifiers=$RATIFIERS"

    # Once propose appears + alice hasn't ratified yet, ratify
    if [ "$PROPOSE" -ge 1 ] && [ "$(events_json "$OP" | python -c "
import sys, json, os
alice = '64c67657-7ec7-476d-95df-cc75c63cee99'
d = json.load(sys.stdin)
print(sum(1 for e in d.get('events', []) if e.get('kind')=='chat.speech.ratify' and e.get('actor_id')==alice))")" -lt 1 ]; then
        note "  operator proposed → alice ratifies"
        curl_post "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice ratifies the proposed list."}
        }' >/dev/null || true
    fi

    # Closure attempt as soon as quorum (2) reached
    if [ "$RATIFIERS" -ge 2 ]; then
        note "  quorum reached — alice attempts close"
        STATUS=$(curl_post -o /tmp/_close_resp -w "%{http_code}" \
            "$BASE/v2/operations/$OP/close" -d '{
                "actor_handle":"@alice","resolution":"answered",
                "summary":"3 ambiguities identified + boundary scenarios per persona quorum"
            }')
        if [ "$STATUS" = "200" ]; then
            note "  close OK"
            break
        fi
        note "  close HTTP=$STATUS body=$(cat /tmp/_close_resp)"
    fi

    # Mid-flight nudge: if no propose by ~3min, prod operator
    if [ "$NUDGED" -eq 0 ] && [ "$PROPOSE" -eq 0 ] && [ "$ELAPSED" -ge "$PROPOSE_NUDGE_AT_S" ]; then
        note "  nudging @operator to propose"
        curl_post "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.question",
            "payload":{"text":"@operator: please post your speech.propose with the final 3 ambiguities now. Use [PROPOSE] prefix in your reply."},
            "addressed_to":"operator",
            "expected_response":{"from_actor_handles":["@operator"],"kinds":["propose","object"]}
        }' >/dev/null || true
        NUDGED=1
    fi

    sleep $PROBE_INTERVAL_S
done

# Force close if not already closed
STATE=$(curl_q "$BASE/v2/operations/$OP" | python -c "import sys, json; print(json.load(sys.stdin)['state'])")
if [ "$STATE" != "closed" ]; then
    note "  state=$STATE not closed — alice forces with abandoned"
    curl_post "$BASE/v2/operations/$OP/close" -d '{
        "actor_handle":"@alice","resolution":"abandoned",
        "summary":"wall-clock budget — quorum not reached"
    }' >/dev/null || true
fi

note ""
note "=== final timeline ==="
dump_timeline "$OP"
FINAL=$(curl_q "$BASE/v2/operations/$OP")
RESOLUTION=$(echo "$FINAL" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('resolution') or '-')")
STATE=$(echo "$FINAL" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('state'))")
note ""
note "=========================================="
note "  state=$STATE resolution=$RESOLUTION"
note "  total events: $(events_json "$OP" | python -c "import sys,json; print(len(json.load(sys.stdin).get('events',[])))")"
note "  propose count: $(count_kind "$OP" chat.speech.propose)"
note "  distinct ratifiers: $(distinct_ratifiers "$OP")"
note "  duration: $(($(date +%s) - START))s"
note "=========================================="
[ "$STATE" = "closed" ] && exit 0 || exit 1
