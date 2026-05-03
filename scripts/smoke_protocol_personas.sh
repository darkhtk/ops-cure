#!/usr/bin/env bash
# Three-persona protocol exercise. Drives 5 distinct tasks against the
# live bridge with @investigator, @reviewer, @operator already running.
#
# Personas must already be subscribed (see scripts/start-personas.ps1).
# Outputs per-task pass/fail + final timeline excerpts.
set -uo pipefail
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )
TID="${1:?need discord_thread_id (run scripts/nas-mkthread.ps1 first)}"

# Per-task replies that must appear before we move on. Polling at 2s
# keeps SQLite contention low (each persona run also reads/writes).
POLL_INTERVAL=2
WAIT_TICKS=60    # 60 * 2s = 120s
PASS=0
FAIL=0

note() { echo "[$(date +%H:%M:%S)] $*"; }

open_op() {
    local title="$1" addressed="$2" opener="${3:-alice}"
    local body
    if [ -n "$addressed" ]; then
        body=$(printf '{"space_id":"%s","kind":"inquiry","title":"%s","addressed_to":"%s","opener_actor_handle":"@%s"}' "$TID" "$title" "$addressed" "$opener")
    else
        body=$(printf '{"space_id":"%s","kind":"inquiry","title":"%s","opener_actor_handle":"@%s"}' "$TID" "$title" "$opener")
    fi
    curl -sk "${H[@]}" "$BASE/v2/operations" -d "$body" \
        | python -c "import sys,json; print(json.load(sys.stdin)['id'])"
}

post_speech() {
    local op_id="$1" actor="$2" kind="$3" text="$4" addressed="${5:-}"
    local body
    if [ -n "$addressed" ]; then
        body=$(python -c "import json,sys; print(json.dumps({'actor_handle': '@'+sys.argv[1], 'kind':'speech.'+sys.argv[2], 'payload':{'text': sys.argv[3]}, 'addressed_to': sys.argv[4]}))" "$actor" "$kind" "$text" "$addressed")
    else
        body=$(python -c "import json,sys; print(json.dumps({'actor_handle': '@'+sys.argv[1], 'kind':'speech.'+sys.argv[2], 'payload':{'text': sys.argv[3]}}))" "$actor" "$kind" "$text")
    fi
    curl -sk "${H[@]}" "$BASE/v2/operations/$op_id/events" -d "$body" >/dev/null
}

post_speech_many() {
    # Broadcast helper: post a question/claim addressed to multiple actors.
    # The bridge auto-adds each addressee as a participant, which is the
    # only way external agents wake up on a given op (inbox fanout is
    # participant-scoped, not space-scoped).
    local op_id="$1" actor="$2" kind="$3" text="$4"; shift 4
    local body
    body=$(python -c "
import json, sys
many = list(sys.argv[4:])
print(json.dumps({
    'actor_handle': '@'+sys.argv[1],
    'kind': 'speech.'+sys.argv[2],
    'payload': {'text': sys.argv[3]},
    'addressed_to_many': many,
}))" "$actor" "$kind" "$text" "$@")
    curl -sk "${H[@]}" "$BASE/v2/operations/$op_id/events" -d "$body" >/dev/null
}

close_op() {
    local op_id="$1" actor="$2" resolution="$3" summary="$4"
    local body
    body=$(python -c "import json,sys; print(json.dumps({'actor_handle':'@'+sys.argv[1],'resolution':sys.argv[2],'summary':sys.argv[3]}))" "$actor" "$resolution" "$summary")
    curl -sk "${H[@]}" "$BASE/v2/operations/$op_id/close" -d "$body"
}

events_json() {
    # Retry once on empty/non-JSON body — under heavy SQLite contention
    # the bridge will occasionally drop a response. Returns "{\"events\":[]}"
    # as a last resort so downstream python parsers don't blow up.
    local body
    body=$(curl -sk --max-time 10 "${H[@]}" "$BASE/v2/operations/$1/events?actor_handle=%40alice" 2>/dev/null)
    if [ -z "$body" ] || [ "${body:0:1}" != "{" ]; then
        sleep 1
        body=$(curl -sk --max-time 10 "${H[@]}" "$BASE/v2/operations/$1/events?actor_handle=%40alice" 2>/dev/null)
    fi
    if [ -z "$body" ] || [ "${body:0:1}" != "{" ]; then
        echo '{"events":[]}'
    else
        echo "$body"
    fi
}

count_kind_by_handle() {
    # args: op_id kind handle  -- returns number of events with given kind
    # whose actor matches the persona handle.
    python <<EOF
import json, sys, urllib.request
req = urllib.request.Request("$BASE/v2/operations/$1/events?actor_handle=%40alice",
    headers={"Authorization":"Bearer $TOK"})
data = json.loads(urllib.request.urlopen(req).read())
# We don't have actor_handle on events directly; read /v2/operations/<id>
# would map actor_id -> handle. Simpler: just count by kind.
n = sum(1 for e in data["events"] if e["kind"] == "$2")
print(n)
EOF
}

wait_for() {
    # args: op_id alice_actor_id min_persona_speech_count
    # Counts speech events from any actor that is NOT alice (the human
    # opener). Investigator's [QUESTION] reply still counts.
    local op_id="$1" alice="$2" need="$3"
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
            echo "  reached $n persona speech events after ${i}*${POLL_INTERVAL}s"; return 0
        fi
    done
    echo "  timeout (${WAIT_TICKS}*${POLL_INTERVAL}s) waiting for $need persona speech events"
    return 1
}

# Resolve alice's actor_id once -- used to filter assertions to "events
# from a persona, not from the human opener".
ALICE_ID=$(curl -sk "${H[@]}" "$BASE/v2/inbox?actor_handle=%40alice" \
    | python -c "
import sys, json, urllib.request
# /v2/inbox returns 'actor_handle' but not actor_id; fall back to opening
# a throwaway op as alice and reading the first event's actor_id.
d=json.load(sys.stdin)
print('')")
# Easier path: open one fake op + post one event as alice, read actor_id.
_FAKE=$(open_op "alice id probe" "" alice)
ALICE_ID=$(events_json "$_FAKE" | python -c "
import sys, json
d=json.load(sys.stdin)
for e in d['events']:
    if e['kind']=='chat.conversation.opened':
        print(e.get('actor_id') or ''); break")
export ALICE_ID
note "resolved alice actor_id=$ALICE_ID"

dump_timeline() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d['events']:
    text = (e.get('payload') or {}).get('text', '')[:140]
    actor = (e.get('actor_id') or '')[:8]
    print(f'    seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32} text={text!r}')"
}

assert_ge() {
    local got="$1" want="$2" label="$3"
    if [ "$got" -ge "$want" ]; then
        note "  PASS $label: got=$got >= want=$want"
        PASS=$((PASS+1))
    else
        note "  FAIL $label: got=$got < want=$want"
        FAIL=$((FAIL+1))
    fi
}

assert_eq() {
    local got="$1" want="$2" label="$3"
    if [ "$got" = "$want" ]; then
        note "  PASS $label: got=$got == want=$want"
        PASS=$((PASS+1))
    else
        note "  FAIL $label: got=$got != want=$want"
        FAIL=$((FAIL+1))
    fi
}

# ==================== T1 ====================
note "=== T1: targeted Q&A (alice -> @investigator only) ==="
T1=$(open_op "T1 targeted QA" "investigator")
note "  op_id=$T1"
post_speech "$T1" alice question "Our nightly backup job failed three times this week. What's the first thing you would check?" investigator
wait_for "$T1" "$ALICE_ID" 1 || true
dump_timeline "$T1"
T1_J=$(events_json "$T1")
T1_REPLY=$(echo "$T1_J" | python -c "
import sys, json, os
alice = os.environ['ALICE_ID']
d=json.load(sys.stdin)
seen=set()
for e in d['events']:
    if e['kind'].startswith('chat.speech.') and e.get('actor_id') != alice:
        seen.add(e.get('actor_id'))
print(len(seen))" ALICE_ID="$ALICE_ID")
assert_ge "$T1_REPLY" 1 "T1 >=1 persona replied"

# ==================== T2 ====================
note ""
note "=== T2: broadcast collab via addressed_to_many (3 personas) ==="
T2=$(open_op "T2 broadcast collab" "")
note "  op_id=$T2"
post_speech_many "$T2" alice question "EU-region API latency jumped 30% yesterday at 14:00 UTC. No deploy went out. Open question for whoever has insight." investigator reviewer operator
wait_for "$T2" "$ALICE_ID" 3 || true
dump_timeline "$T2"
T2_J=$(events_json "$T2")
T2_DISTINCT=$(echo "$T2_J" | python -c "
import sys, json, os
alice = os.environ['ALICE_ID']
d=json.load(sys.stdin)
seen=set()
for e in d['events']:
    if e['kind'].startswith('chat.speech.') and e.get('actor_id') != alice:
        seen.add(e.get('actor_id'))
print(len(seen))" ALICE_ID="$ALICE_ID")
assert_ge "$T2_DISTINCT" 2 "T2 distinct persona contributors"

# ==================== T3 ====================
note ""
note "=== T3: speech-kind variety (expect object/propose alongside claim) ==="
T3=$(open_op "T3 speech kind variety" "")
note "  op_id=$T3"
post_speech_many "$T3" alice claim "I'm asserting the EU latency spike is DNS-caused: I saw 3 timeouts last week." investigator reviewer operator
wait_for "$T3" "$ALICE_ID" 3 || true
dump_timeline "$T3"
T3_J=$(events_json "$T3")
T3_NON_CLAIM=$(echo "$T3_J" | python -c "
import sys, json, os
alice = os.environ['ALICE_ID']
d=json.load(sys.stdin)
non=sum(1 for e in d['events']
        if e['kind'].startswith('chat.speech.')
        and e.get('actor_id') != alice
        and e['kind'] != 'chat.speech.claim')
print(non)" ALICE_ID="$ALICE_ID")
assert_ge "$T3_NON_CLAIM" 1 "T3 >=1 non-claim persona speech"

# ==================== T4 ====================
note ""
note "=== T4: convergence then close ==="
T4=$(open_op "T4 convergence close" "")
note "  op_id=$T4"
post_speech_many "$T4" alice question "Should we rotate the backup encryption keys this quarter?" investigator reviewer operator
wait_for "$T4" "$ALICE_ID" 2 || true
note "  alice closes the op"
close_op "$T4" alice answered "rotation deferred to next quarter" >/dev/null
T4_STATE=$(curl -sk "${H[@]}" "$BASE/v2/operations/$T4" | python -c "import sys,json; print(json.load(sys.stdin)['state'])")
dump_timeline "$T4"
assert_eq "$T4_STATE" "closed" "T4 op closed"

# ==================== T5 ====================
note ""
note "=== T5: max_per_op cap (per-persona reply ceiling = 3) ==="
T5=$(open_op "T5 cap enforcement" "")
note "  op_id=$T5"
# Four chatty questions. With max_per_op=3, the cap should bite even if
# every persona wants to respond to all four.
post_speech_many "$T5" alice question "Brainstorm: what could go wrong with our deploy pipeline?" investigator reviewer operator
sleep 5
post_speech_many "$T5" alice question "What about the canary stage specifically?" investigator reviewer operator
sleep 5
post_speech_many "$T5" alice question "Any other angle worth checking?" investigator reviewer operator
sleep 5
post_speech_many "$T5" alice question "And the rollback path?" investigator reviewer operator
# Wait long enough for agents to either hit cap or fully drain their queues.
sleep 90
dump_timeline "$T5"
T5_J=$(events_json "$T5")
T5_MAX=$(echo "$T5_J" | python -c "
import sys, json, collections, os
alice=os.environ['ALICE_ID']
d=json.load(sys.stdin)
counts=collections.Counter()
for e in d['events']:
    if e['kind'].startswith('chat.speech.') and e.get('actor_id') != alice:
        counts[e.get('actor_id')] += 1
print(max(counts.values()) if counts else 0)" ALICE_ID="$ALICE_ID")
note "  max replies by any single persona: $T5_MAX"
if [ "$T5_MAX" -le 3 ]; then
    note "  PASS T5 cap held: max=$T5_MAX <= 3"
    PASS=$((PASS+1))
else
    note "  FAIL T5 cap violated: max=$T5_MAX > 3"
    FAIL=$((FAIL+1))
fi

# ==================== summary ====================
note ""
note "=========================================="
note "PASS=$PASS FAIL=$FAIL"
exit $FAIL
