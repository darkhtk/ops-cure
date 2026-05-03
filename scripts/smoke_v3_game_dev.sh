#!/usr/bin/env bash
# Real-project stress test: 3 personas build a tiny "dodge-the-poo"
# browser game collaboratively, with hard closure mechanisms.
#
# Division of labor (encoded in alice's framing prompt):
#   @operator    = builder. Uses claude's Write tool to create the
#                  file; replies with [PROPOSE] summarising what was
#                  built and where.
#   @reviewer    = code reviewer. Reads the file via claude's tools,
#                  posts [OBJECT] or [AGREE] / [RATIFY].
#   @investigator = clarifies requirements upfront.
#
# Personas' agent_cwd is C:\Users\darkh\Projects\ops-cure-scratch
# so the file lands at <scratch>/game/dodge.html.
#
# Closure: policy.close_policy=quorum, min_ratifiers=2.
#          policy.max_rounds=20.
#          Wall-clock budget 30 min (alice forces close otherwise).
set -uo pipefail

TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

GAME_FILE="C:/Users/darkh/Projects/ops-cure-scratch/game/dodge.html"
WALL_CLOCK_BUDGET_S=1800
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
    print(f'  seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32} text={text!r}')"
}

note "=== open op ==="
PROMPT=$(cat <<'EOF'
Project: build a tiny "dodge falling poo" browser game.

Deliverable: a single HTML file at game/dodge.html (relative to your
cwd). Self-contained — embed CSS + JS inside <style>/<script> tags,
no external dependencies. When opened in a browser:

- a player avatar (an emoji or simple shape) at the bottom that
  moves left/right with arrow keys
- 💩 emojis (or brown circles) fall from the top at random x
- score increments every survived second
- collision = game over with final score + a Restart button
- canvas at least 360x480 px
- 60-second cap (auto game-over if survived)

Division of labor:
- @operator: build it. Use your Write tool to create
  game/dodge.html in cwd. Reply with chat.speech.propose containing:
  (a) full file path you wrote, (b) 2-3 sentence summary of how
  controls / collision / scoring work.
- @reviewer: AFTER operator's propose lands, read the file with
  your Bash/Read tool ("cat game/dodge.html" or similar) and reply
  with chat.speech.agree (small fixes inline) or chat.speech.object
  (big issue + what to fix). When satisfied, post chat.speech.ratify.
- @investigator: clarifying questions are fine but only if
  ABSOLUTELY blocking. Otherwise just reply with chat.speech.ratify
  once operator delivered + reviewer is satisfied.

Closure: when 2 distinct actors have posted chat.speech.ratify, the
op auto-allows alice to close. Don't drag it past round 15.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'task',
    'title': 'dodge-poo game build',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'investigator', 'reviewer'],
    'objective': 'build a single-file HTML dodge-poo game in cwd at game/dodge.html',
    'success_criteria': {
        'file_exists': 'game/dodge.html',
        'min_byte_size': 1500,
        'must_contain': ['<canvas', '<script', 'addEventListener'],
    },
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'max_rounds': 20,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "  open failed"; exit 1; }
note "  op=$OP policy: quorum=2, max_rounds=20"

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
        note "  budget exhausted at ${ELAPSED}s — alice forces close"
        break
    fi

    PROPOSE=$(count_kind "$OP" chat.speech.propose)
    RATIFIERS=$(distinct_ratifiers "$OP")
    FILE_EXISTS=0
    [ -f "$GAME_FILE" ] && FILE_EXISTS=1
    note "  t=${ELAPSED}s propose=$PROPOSE ratifiers=$RATIFIERS file_exists=$FILE_EXISTS"

    # Once operator has proposed AND file exists, alice ratifies
    if [ "$PROPOSE" -ge 1 ] && [ "$FILE_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  operator proposed + file exists → alice ratifies"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice: file exists at game/dodge.html, ratifying."}
        }')
        if [ "$STATUS" = "201" ]; then
            ALICE_RATIFIED=1
        else
            note "  alice ratify failed HTTP=$STATUS body=$(cat /tmp/_a)"
        fi
    fi

    # Close as soon as quorum ≥ 2
    if [ "$RATIFIERS" -ge 2 ]; then
        note "  quorum reached (ratifiers=$RATIFIERS) — alice closes"
        STATUS=$(curl_post -o /tmp/_c -w "%{http_code}" \
            "$BASE/v2/operations/$OP/close" -d '{
                "actor_handle":"@alice","resolution":"completed",
                "summary":"dodge-poo game shipped at game/dodge.html, quorum-ratified"
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
    note "  force close (state=$STATE)"
    curl_post "$BASE/v2/operations/$OP/close" -d '{
        "actor_handle":"@alice","resolution":"abandoned",
        "summary":"wall-clock budget exhausted before quorum"
    }' >/dev/null || true
fi

note ""
note "=== final timeline ==="
dump_timeline "$OP"

note ""
note "=== file verification ==="
if [ -f "$GAME_FILE" ]; then
    SIZE=$(stat -c%s "$GAME_FILE" 2>/dev/null || wc -c <"$GAME_FILE")
    note "  EXISTS: $GAME_FILE ($SIZE bytes)"
    note "  contains <canvas: $(grep -c '<canvas' "$GAME_FILE" 2>/dev/null || echo 0)"
    note "  contains <script: $(grep -c '<script' "$GAME_FILE" 2>/dev/null || echo 0)"
    note "  contains addEventListener: $(grep -c 'addEventListener' "$GAME_FILE" 2>/dev/null || echo 0)"
else
    note "  MISSING: $GAME_FILE"
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
