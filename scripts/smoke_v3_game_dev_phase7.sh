#!/usr/bin/env bash
# Phase 7 production-flow smoke: 3 personas build a tiny dodge-poo
# browser game with hard, deliverable-aware closure.
#
# Differences from smoke_v3_game_dev.sh:
#   - kind=task uses /v2/operations default ``bind_remote_task=false``
#     (T1.1) → no v1 task-guard blocks the close path
#   - policy.requires_artifact=true (T2.1) → close is rejected until
#     ≥1 OperationArtifact is attached
#   - Prompt instructs operator to use the ``ARTIFACT: path=...``
#     header on speech.evidence (T1.2) so the bridge auto-creates
#     the artifact row
#   - Smoke verifies via GET /v2/operations/{id}/artifacts that the
#     deliverable was formally attached
#
# Closure: quorum=2 + requires_artifact. Wall-clock 30 min.
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
artifacts_json() { curl_q "$BASE/v2/operations/$1/artifacts"; }

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

artifact_count() {
    artifacts_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
print(len(d.get('artifacts', [])))"
}

dump_timeline() {
    events_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('events', []):
    actor = (e.get('actor_id') or '')[:8]
    text = (e.get('payload') or {}).get('text', '')[:160]
    has_art = 'artifact' in (e.get('payload') or {})
    art = ' [ART]' if has_art else ''
    print(f'  seq={e[\"seq\"]:>2} actor={actor:<8} kind={e[\"kind\"]:<32}{art} text={text!r}')"
}

dump_artifacts() {
    artifacts_json "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('artifacts', []):
    print(f'  artifact id={a[\"id\"][:8]} kind={a[\"kind\"]:<10} '
          f'size={a[\"size_bytes\"]:>6} sha={a[\"sha256\"][:12]}.. '
          f'mime={a[\"mime\"]:<24} uri={a[\"uri\"]}')"
}

note "=== open op (kind=task, bind_remote_task=false default, requires_artifact=true) ==="
PROMPT=$(cat <<'EOF'
Project: build a tiny "dodge falling poo" browser game.

Deliverable: a single self-contained HTML file at game/dodge.html
(relative to your cwd). Embed CSS + JS inline; no external deps.
When opened in a browser:

- player avatar at the bottom moves left/right with arrow keys
- 💩 emojis fall from the top at random x positions
- score increments every survived second
- collision = game over screen with final score + Restart button
- canvas at least 360x480 px

This op uses production closure rules (Phase 7):
- close_policy=quorum, min_ratifiers=2
- requires_artifact=true → cannot close without an OperationArtifact
  attached. The artifact MUST come via @operator's speech.evidence.

Division of labor:
- @operator: WRITE game/dodge.html with the claude Write tool. Then
  reply with [EVIDENCE→@reviewer,@investigator]. ON THE SECOND LINE
  of the body, include the ARTIFACT header so the bridge attaches
  the file as a formal OperationArtifact:

      [EVIDENCE→@reviewer,@investigator]
      ARTIFACT: path=game/dodge.html kind=code label="dodge v1"
      Wrote dodge.html — controls / collision / scoring overview...

  After the evidence is delivered, the artifact will be queryable
  via GET /v2/operations/{id}/artifacts and quorum-close becomes
  unblocked.
- @reviewer: AFTER operator's evidence lands, read the file with
  your Read tool. Reply with [OBJECT→@operator] (specific fix) OR
  [RATIFY→@alice] when satisfied.
- @investigator: clarifying question only if blocking. Otherwise
  [RATIFY→@alice] once operator delivered + reviewer is satisfied.

Closure: 2 distinct ratifiers + artifact attached → alice closes.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'task',
    'title': 'phase7 dodge-poo (artifact-aware)',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'investigator', 'reviewer'],
    'objective': 'build a single-file HTML dodge-poo game with formal artifact attach',
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'requires_artifact': True,
        'max_rounds': 20,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "  open failed"; exit 1; }
note "  op=$OP policy: quorum=2, requires_artifact=true, max_rounds=20"

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

    EVIDENCE=$(count_kind "$OP" chat.speech.evidence)
    PROPOSE=$(count_kind "$OP" chat.speech.propose)
    RATIFIERS=$(distinct_ratifiers "$OP")
    ARTIFACTS=$(artifact_count "$OP")
    FILE_EXISTS=0
    [ -f "$GAME_FILE" ] && FILE_EXISTS=1
    note "  t=${ELAPSED}s evidence=$EVIDENCE propose=$PROPOSE ratifiers=$RATIFIERS artifacts=$ARTIFACTS file_exists=$FILE_EXISTS"

    # Once an artifact exists AND file is on disk, alice ratifies
    if [ "$ARTIFACTS" -ge 1 ] && [ "$FILE_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  artifact attached + file on disk → alice ratifies"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice: artifact attached, ratifying."}
        }')
        if [ "$STATUS" = "201" ]; then
            ALICE_RATIFIED=1
        else
            note "  alice ratify failed HTTP=$STATUS body=$(cat /tmp/_a)"
        fi
    fi

    # Close as soon as quorum ≥ 2 (requires_artifact gate also applies)
    if [ "$RATIFIERS" -ge 2 ]; then
        note "  quorum reached (ratifiers=$RATIFIERS, artifacts=$ARTIFACTS) — alice closes"
        STATUS=$(curl_post -o /tmp/_c -w "%{http_code}" \
            "$BASE/v2/operations/$OP/close" -d '{
                "actor_handle":"@alice","resolution":"completed",
                "summary":"dodge game shipped + formally attached as artifact"
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
        "summary":"wall-clock budget exhausted"
    }' >/dev/null || true
fi

note ""
note "=== final timeline ==="
dump_timeline "$OP"

note ""
note "=== artifacts attached ==="
dump_artifacts "$OP"

note ""
note "=== file on disk ==="
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
ARTIFACT_FINAL=$(artifact_count "$OP")
note ""
note "=========================================="
note "  state=$STATE resolution=$RESOLUTION"
note "  artifacts attached: $ARTIFACT_FINAL"
note "  total events: $(events_json "$OP" | python -c "import sys,json; print(len(json.load(sys.stdin).get('events',[])))")"
note "  duration: $((NOW - START))s"
note "=========================================="
