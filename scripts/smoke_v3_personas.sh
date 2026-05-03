#!/usr/bin/env bash
# Live re-run of the 3-persona / 5-task exercise against v3.
#
# Each task explicitly leverages a v3 primitive that didn't exist
# during the original exercise:
#   T1  expected_response.from_actor_handles → cascade prevention
#   T2  addressed_to_many → multi-participant collab
#   T3  expected_response.kinds → kind whitelist enforcement
#   T4  close_policy=any_participant → close gate at protocol layer
#   T5  policy.max_rounds → server-side reply cap
#
# Personas (@investigator, @reviewer, @operator) must already be
# running (see scripts/start-personas.ps1) and subscribed to their
# v2 inbox SSE.
set -uo pipefail
TID="${1:?need discord_thread_id (run scripts/nas-mkthread.ps1)}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

PASS=0
FAIL=0
WAIT_TICKS=60   # 60 * 2s = 120s per wait
POLL_INTERVAL=2

note() { echo "[$(date +%H:%M:%S)] $*"; }

events_json() {
    local body
    body=$(curl -sk --max-time 10 "${H[@]}" "$BASE/v2/operations/$1/events?actor_handle=%40alice" 2>/dev/null)
    if [ -z "$body" ] || [ "${body:0:1}" != "{" ]; then
        sleep 1
        body=$(curl -sk --max-time 10 "${H[@]}" "$BASE/v2/operations/$1/events?actor_handle=%40alice" 2>/dev/null)
    fi
    [ -z "$body" ] && body='{"events":[]}'
    echo "$body"
}

dump_timeline() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('events', []):
    text = (e.get('payload') or {}).get('text', '')[:120]
    actor = (e.get('actor_id') or '')[:8]
    ex = e.get('expected_response')
    ex_str = '' if not ex else f' expected_from={ex.get(\"from_actor_handles\",[])}'
    print(f'    seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32} text={text!r}{ex_str}')"
}

ALICE_ID=$(curl -sk "${H[@]}" "$BASE/v2/operations" \
    -d "{\"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"alice id probe\",\"opener_actor_handle\":\"@alice\"}" \
    | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))")
ALICE_OP=$ALICE_ID
ALICE_ID=$(events_json "$ALICE_OP" | python -c "
import sys, json
d=json.load(sys.stdin)
for e in d.get('events', []):
    if e.get('kind')=='chat.conversation.opened':
        print(e.get('actor_id') or ''); break")
export ALICE_ID
note "resolved alice actor_id=$ALICE_ID"

wait_for_speech() {
    # args: op_id min_speech_count
    local op_id="$1" need="$2"
    for i in $(seq 1 $WAIT_TICKS); do
        sleep $POLL_INTERVAL
        local n
        n=$(events_json "$op_id" | python -c "
import sys, json, os
alice = os.environ.get('ALICE_ID','')
d = json.load(sys.stdin)
print(sum(1 for e in d.get('events', [])
         if e.get('kind','').startswith('chat.speech.')
         and e.get('actor_id') != alice))" 2>/dev/null)
        n=${n:-0}
        if [ "$n" -ge "$need" ]; then
            echo "  reached $n persona speech events after ${i}*${POLL_INTERVAL}s"
            return 0
        fi
    done
    echo "  timeout waiting for $need persona speech events"
    return 1
}

count_persona_actors() {
    events_json "$1" | python -c "
import sys, json, os
alice=os.environ.get('ALICE_ID','')
d=json.load(sys.stdin)
seen=set()
for e in d.get('events', []):
    if e.get('kind','').startswith('chat.speech.') and e.get('actor_id') != alice:
        seen.add(e.get('actor_id'))
print(len(seen))"
}

count_speech_kinds() {
    # arg: op_id; emits 'kind:count' lines for non-alice events
    events_json "$1" | python -c "
import sys, json, os, collections
alice=os.environ.get('ALICE_ID','')
d=json.load(sys.stdin)
c = collections.Counter()
for e in d.get('events', []):
    if e.get('kind','').startswith('chat.speech.') and e.get('actor_id') != alice:
        c[e['kind']] += 1
for k, v in sorted(c.items()):
    print(f'{k}:{v}')"
}

count_speech_total() {
    events_json "$1" | python -c "
import sys, json
d=json.load(sys.stdin)
print(sum(1 for e in d.get('events', []) if e.get('kind','').startswith('chat.speech.')))"
}

assert_ge() { [ "$1" -ge "$2" ] && { note "  PASS $3 ($1 >= $2)"; PASS=$((PASS+1)); } || { note "  FAIL $3 ($1 < $2)"; FAIL=$((FAIL+1)); }; }
assert_le() { [ "$1" -le "$2" ] && { note "  PASS $3 ($1 <= $2)"; PASS=$((PASS+1)); } || { note "  FAIL $3 ($1 > $2)"; FAIL=$((FAIL+1)); }; }
assert_eq() { [ "$1" = "$2" ] && { note "  PASS $3 ($1 == $2)"; PASS=$((PASS+1)); } || { note "  FAIL $3 (got=$1 want=$2)"; FAIL=$((FAIL+1)); }; }

# ============================================================================
# T1 — expected_response.from_actor_handles → cascade prevention
# ============================================================================
note "=== T1: expected_response targets only @investigator → only investigator replies ==="
T1=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T1 expected_response\",
    \"opener_actor_handle\":\"@alice\",
    \"addressed_to\":\"investigator\"
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op_id=$T1"
curl -sk "${H[@]}" "$BASE/v2/operations/$T1/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
    \"payload\":{\"text\":\"Backups failed three nights running. Where would you start?\"},
    \"addressed_to\":\"investigator\",
    \"expected_response\":{\"from_actor_handles\":[\"@investigator\"]}
}" >/dev/null
wait_for_speech "$T1" 1 || true
sleep 5  # give reviewer/operator a chance to (incorrectly) chime in if they would
dump_timeline "$T1"
T1_ACTORS=$(count_persona_actors "$T1")
note "  distinct persona contributors: $T1_ACTORS"
assert_eq "$T1_ACTORS" 1 "T1 only one persona replied (cascade prevented)"

# ============================================================================
# T2 — addressed_to_many → 3 personas all see + reply
# ============================================================================
note ""
note "=== T2: addressed_to_many[i,r,o] → all 3 personas contribute ==="
T2=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T2 broadcast\",
    \"opener_actor_handle\":\"@alice\"
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op_id=$T2"
curl -sk "${H[@]}" "$BASE/v2/operations/$T2/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
    \"payload\":{\"text\":\"EU latency jumped 30% at 14:00 UTC. Open question for any insight.\"},
    \"addressed_to_many\":[\"investigator\",\"reviewer\",\"operator\"],
    \"expected_response\":{\"from_actor_handles\":[\"@investigator\",\"@reviewer\",\"@operator\"]}
}" >/dev/null
wait_for_speech "$T2" 3 || true
dump_timeline "$T2"
T2_ACTORS=$(count_persona_actors "$T2")
note "  distinct persona contributors: $T2_ACTORS"
assert_ge "$T2_ACTORS" 2 "T2 at least 2 personas contributed"

# ============================================================================
# T3 — expected_response.kinds whitelist
# ============================================================================
note ""
note "=== T3: expected_response.kinds=[object,question,react] enforces server-side ==="
T3=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T3 kind whitelist\",
    \"opener_actor_handle\":\"@alice\"
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op_id=$T3"
# alice asserts a controversial claim and invites object/question/react.
# A persona that tries to reply with [CLAIM] (default) gets rejected.
Q3=$(curl -sk "${H[@]}" "$BASE/v2/operations/$T3/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.claim\",
    \"payload\":{\"text\":\"I assert the EU latency spike is DNS-caused (3 timeouts last week).\"},
    \"addressed_to_many\":[\"reviewer\",\"investigator\",\"operator\"],
    \"expected_response\":{\"from_actor_handles\":[\"@reviewer\",\"@investigator\",\"@operator\"],\"kinds\":[\"object\",\"question\",\"react\"]}
}" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))")
note "  trigger event_id=$Q3"
wait_for_speech "$T3" 2 || true
dump_timeline "$T3"
note "  speech kind distribution:"
count_speech_kinds "$T3" | sed 's/^/    /'
# Any persona event posted should be one of object/question/react. claim should NOT appear.
T3_CLAIM_COUNT=$(count_speech_kinds "$T3" | grep -E '^chat\.speech\.claim:' | awk -F: '{s+=$2} END {print s+0}')
assert_eq "$T3_CLAIM_COUNT" 0 "T3 no persona claim slipped through"

# ============================================================================
# T4 — close_policy=any_participant
# ============================================================================
note ""
note "=== T4: close_policy=any_participant → alice (participant) closes after collab ==="
T4=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T4 close any_participant\",
    \"opener_actor_handle\":\"@alice\",
    \"policy\":{\"close_policy\":\"any_participant\"}
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op_id=$T4"
curl -sk "${H[@]}" "$BASE/v2/operations/$T4/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
    \"payload\":{\"text\":\"Should we rotate backup encryption keys this quarter?\"},
    \"addressed_to_many\":[\"investigator\",\"operator\"],
    \"expected_response\":{\"from_actor_handles\":[\"@investigator\",\"@operator\"]}
}" >/dev/null
wait_for_speech "$T4" 1 || true
sleep 5
dump_timeline "$T4"
# alice closes the op
T4_CLOSE=$(curl -sk -o /tmp/_t4_close -w "%{http_code}" "${H[@]}" "$BASE/v2/operations/$T4/close" \
    -d '{"actor_handle":"@alice","resolution":"answered","summary":"deferred to next quarter"}')
note "  close HTTP=$T4_CLOSE"
T4_STATE=$(curl -sk "${H[@]}" "$BASE/v2/operations/$T4" | python -c "import sys,json; print(json.load(sys.stdin)['state'])")
assert_eq "$T4_STATE" "closed" "T4 state=closed"

# ============================================================================
# T5 — max_rounds cap (server-side)
# ============================================================================
note ""
note "=== T5: policy.max_rounds=4 caps total speech events ==="
T5=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T5 max_rounds\",
    \"opener_actor_handle\":\"@alice\",
    \"policy\":{\"max_rounds\":4}
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op_id=$T5"
# alice posts the FIRST speech event (this counts as 1 of 4)
curl -sk "${H[@]}" "$BASE/v2/operations/$T5/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
    \"payload\":{\"text\":\"Brainstorm: what could go wrong with our deploy pipeline?\"},
    \"addressed_to_many\":[\"investigator\",\"reviewer\",\"operator\"],
    \"expected_response\":{\"from_actor_handles\":[\"@investigator\",\"@reviewer\",\"@operator\"]}
}" >/dev/null
sleep 5
# Try posting more questions; bridge will reject once cap hit.
for i in 2 3 4 5 6; do
    rc=$(curl -sk -o /dev/null -w "%{http_code}" "${H[@]}" "$BASE/v2/operations/$T5/events" -d "{
        \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
        \"payload\":{\"text\":\"Followup #$i?\"},
        \"addressed_to\":\"investigator\"
    }")
    note "  alice followup #$i HTTP=$rc"
done
sleep 30
dump_timeline "$T5"
T5_TOTAL=$(count_speech_total "$T5")
note "  total speech events on op: $T5_TOTAL"
assert_le "$T5_TOTAL" 4 "T5 max_rounds=4 cap held"

# ============================================================================
note ""
note "=========================================="
note "PASS=$PASS FAIL=$FAIL"
exit $FAIL
