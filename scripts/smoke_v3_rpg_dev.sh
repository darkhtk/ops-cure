#!/usr/bin/env bash
# RPG dev — 4 personas (operator + reviewer + designer + investigator)
# build a tiny single-file browser RPG under Phase 7 closure rules.
#
# Diff vs smoke_v3_game_dev_phase7.sh:
#   - 4 personas instead of 3 (@designer added: stats / balance /
#     encounter pacing / win-lose conditions, NO code)
#   - bigger budget (60 min wall clock, 30 rounds) — RPG > dodge-poo
#   - deliverable at rpg/quest.html (canvas + DOM mix is fine)
#
# Phase 7 closure (unchanged):
#   - kind=task, bind_remote_task=false (T1.1 default via /v2/operations)
#   - close_policy=quorum, min_ratifiers=2
#   - requires_artifact=true (T2.1) — close blocked until OperationArtifact attached
#   - operator's [EVIDENCE] reply uses ARTIFACT header (T1.2)
set -uo pipefail

TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

GAME_FILE="C:/Users/darkh/Projects/ops-cure-scratch/rpg/quest.html"
WALL_CLOCK_BUDGET_S=3600
PROBE_INTERVAL_S=10

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

note "=== open op (4 personas — operator, reviewer, designer, investigator) ==="
PROMPT=$(cat <<'EOF'
Project: build a tiny single-file browser RPG.

Deliverable: a self-contained HTML file at rpg/quest.html (relative
to your cwd). Embed CSS + JS inline; no external deps. Open in a
browser, get a playable RPG.

REQUIRED features (the bar for "tiny RPG", not Skyrim):
- Player: HP, MP, level, XP, position on a small grid (8x8 ok)
- Movement: arrow keys to walk on the grid; obstacles block
- At least 2 enemy types with distinct stats; encounter on collision
- Turn-based combat panel: attack / magic / item / flee. HP/MP shown.
- Inventory: at least HP potion (drop from enemies). Use during combat.
- Level-up on XP threshold; HP/MP/attack scale with level
- Win condition (defeat boss enemy or reach a goal tile)
- Lose condition (HP=0 → game over with restart button)

Phase 7 closure rules:
- close_policy=quorum, min_ratifiers=2
- requires_artifact=true → cannot close until ≥1 OperationArtifact
  attached. The deliverable file MUST come via @operator's
  [EVIDENCE] reply with the ARTIFACT header.

Division of labor (4 roles):
- @designer: define before operator codes — HP/MP/damage formulas,
  XP curve, enemy stats, drop rates, win condition specifics, UI
  layout. Use [PROPOSE→@operator,@alice kinds=ratify,object] to
  surface a design decision; the others ratify or counter. Do NOT
  write code yourself. When operator's [EVIDENCE] lands, audit
  that the implementation matches the ratified design — challenge
  with [OBJECT→@operator] if it diverges.
- @operator: build it. After @designer's first proposal lands and
  is ratified, write rpg/quest.html. Reply with:

      [EVIDENCE→@reviewer,@designer]
      ARTIFACT: path=rpg/quest.html kind=code label="quest v1"
      Wrote rpg/quest.html. Implements: <bullet summary mapping
      design decisions → code locations>.

  If the design isn't fully resolved when you'd otherwise build,
  use [QUESTION→@designer kinds=propose] to ask, don't guess.
- @reviewer: AFTER operator's evidence lands, read the file and
  reply [OBJECT→@operator] (concrete fix) or [RATIFY→@alice]. You
  check CODE quality — bugs, edge cases, accessibility, dead code.
  Design quality is @designer's job.
- @investigator: only if a design decision lacks evidence (e.g.
  operator coded a value that designer never proposed). Otherwise
  [RATIFY→@alice] once you're satisfied.

Closure: 2 distinct ratifiers + artifact attached → alice closes.
Don't drag past round 30.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'task',
    'title': 'phase7 RPG (4 personas, artifact-aware)',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'designer', 'reviewer', 'investigator'],
    'objective': 'build a tiny single-file browser RPG with formal artifact attach',
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'requires_artifact': True,
        'max_rounds': 30,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "  open failed"; exit 1; }
note "  op=$OP policy: quorum=2, requires_artifact=true, max_rounds=30"

curl_post "$BASE/v2/operations/$OP/events" -d "$(python -c "
import json, sys
print(json.dumps({
    'actor_handle': '@alice',
    'kind': 'speech.question',
    'payload': {'text': '''$PROMPT'''},
    'addressed_to_many': ['operator', 'designer', 'reviewer', 'investigator'],
    'expected_response': {
        'from_actor_handles': ['@operator', '@designer', '@reviewer', '@investigator'],
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
    OBJECT=$(count_kind "$OP" chat.speech.object)
    RATIFIERS=$(distinct_ratifiers "$OP")
    ARTIFACTS=$(artifact_count "$OP")
    FILE_EXISTS=0
    [ -f "$GAME_FILE" ] && FILE_EXISTS=1
    note "  t=${ELAPSED}s ev=$EVIDENCE prop=$PROPOSE obj=$OBJECT ratifiers=$RATIFIERS artifacts=$ARTIFACTS file=$FILE_EXISTS"

    # Once an artifact exists AND file is on disk, alice ratifies
    if [ "$ARTIFACTS" -ge 1 ] && [ "$FILE_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  artifact attached + file on disk → alice ratifies"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice: artifact attached at rpg/quest.html, ratifying."}
        }')
        if [ "$STATUS" = "201" ]; then
            ALICE_RATIFIED=1
        else
            note "  alice ratify failed HTTP=$STATUS body=$(cat /tmp/_a)"
        fi
    fi

    if [ "$RATIFIERS" -ge 2 ]; then
        note "  quorum reached (ratifiers=$RATIFIERS, artifacts=$ARTIFACTS) — alice closes"
        STATUS=$(curl_post -o /tmp/_c -w "%{http_code}" \
            "$BASE/v2/operations/$OP/close" -d '{
                "actor_handle":"@alice","resolution":"completed",
                "summary":"RPG shipped at rpg/quest.html, artifact-attached, quorum-ratified"
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
    note "  HP regex: $(grep -cE '\\bHP\\b|\\bhp\\b' "$GAME_FILE" 2>/dev/null || echo 0)"
    note "  enemy regex: $(grep -ci 'enemy\\|monster\\|combat' "$GAME_FILE" 2>/dev/null || echo 0)"
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
