#!/usr/bin/env bash
# Godot volleyball clone — 4 personas build a 2-player volleyball
# game with original mechanics but DIFFERENT VISUAL DESIGN.
#
# Reference (mechanics only — replicate, don't copy art):
#   classic 2-player Pikachu Volleyball
#     - 1v1 court, ball bounces with simple physics
#     - players move left/right, jump, power-spike at apex
#     - first to 15 (win by 2), best of 3 sets
#     - AI opponent for player vs CPU
#
# Design constraint: NOT Pokemon. Pick any other theme — robots,
# aliens, mushrooms, food, planets — as long as the gameplay
# feel is identical to the original.
#
# Architecture (no Godot editor needed at runtime):
#   - Agents write GDScript + .tscn via filesystem in cwd
#   - Run via:  Godot.exe --headless --path . --quit-after <N>
#   - Autoplay harness (a Node attached to Main.tscn) reads
#     command-line args (--automation --duration N --out path),
#     drives both players via simple AI, captures PNG via
#     get_viewport().get_texture().get_image().save_png(),
#     writes JSON summary, calls get_tree().quit()
#
# Phase 7 closure (unchanged):
#   - kind=task, bind_remote_task=false default (T1.1)
#   - close_policy=quorum, min_ratifiers=2
#   - requires_artifact=true (T2.1)
#   - operator's [EVIDENCE] uses one or more ARTIFACT headers
#     (P9.3 multi-artifact) on each deliverable
#   - non-success resolutions (abandoned/failed/cancelled) bypass
#     requires_artifact (P10.4) so a stuck op can still close
set -uo pipefail

TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

PROJECT_CWD="C:/Users/darkh/Projects/ops-cure-scratch/GodotVolleyball"
WALL_CLOCK_BUDGET_S=5400
PROBE_INTERVAL_S=15

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

note "=== open op (kind=task, Godot volleyball clone) ==="
PROMPT=$(cat <<'EOF'
Project: build a single-player volleyball game in Godot 4.6 that
replicates the gameplay of classic Pikachu Volleyball with a
COMPLETELY DIFFERENT visual theme. Mechanics same, art / characters
/ flavor different.

Reference (mechanics, NOT visuals):
  - 1v1 court split by a net
  - 2 characters, one human-controlled, one AI-controlled
  - ball physics: gravity, bounces off walls + characters, no spin
  - controls: ←/→ to move, ↑ to jump (single jump), Space to spike
    (only effective when airborne and near the ball)
  - serving: alternates between sides per set
  - score: first to 15 wins a set; win by 2; first to 2 sets wins
    the match
  - simple AI: tracks ball x-coord, jumps when ball is overhead,
    spikes when at apex near ball

VISUAL DESIGN — your call. Examples (pick ONE, don't infringe):
  - aliens (red blob vs green blob on a moon court)
  - vegetables (carrot vs broccoli on a field)
  - robots (bot-A vs bot-B in a steel arena)
  - planets (Mars vs Saturn under a starfield)
  - polish: 2D pixel art OR clean vector shapes — whichever fits
    the theme. Use Godot's built-in primitives (ColorRect /
    Polygon2D / Sprite2D with code-generated textures) so we don't
    need external assets.

INFRASTRUCTURE PROVIDED:
  project.godot (4.6 stable, GL Compatibility renderer, main scene
  expected at res://scenes/Main.tscn)

YOU OWN (filesystem-only, no Godot editor needed):
  1. scenes/Main.tscn — root scene with court, players, ball, score
     UI, AutomationHarness attached
  2. scripts/Player.gd, scripts/Ball.gd, scripts/AI.gd,
     scripts/MatchController.gd — game logic
  3. scripts/AutomationHarness.gd — reads cmdline args
     (--automation, --duration <s>, --out <path>,
     --capture <comma-list>), drives both players via simple AI,
     captures PNG screenshots via
       img = get_viewport().get_texture().get_image()
       img.save_png(path)
     writes JSON summary, calls get_tree().quit() at end
  4. README.md — how to play + how autoplay works

GODOT BINARY (use exactly):
  "/c/Users/darkh/AppData/Local/Microsoft/WinGet/Packages/GodotEngine.GodotEngine_Microsoft.Winget.Source_8wekyb3d8bbwe/Godot_v4.6.2-stable_win64_console.exe"

RUN headless autoplay (use this shape; tweak args if your harness
uses different flags):
  Godot.exe --headless --path . -- \
    --automation --duration 30 \
    --out autoplay-summary.json \
    --capture 5,15,28

PRE-FLIGHT (rev 9 / D12): @designer should list domain assumptions
before any code (Godot 4.6 GDScript syntax differences from 4.x,
headless rendering caveats, screenshot path resolution, etc.) so
@operator doesn't guess.

MANDATORY VERIFICATION (rev 5 / D11 / D12):
  Two of you (reviewer + designer, OR designer + investigator)
  must read autoplay-summary.json + at least one screenshot
  before [RATIFY]. Failure modes counted as deviations:
    - status="failed"
    - score reads 0/0 with no goals after 30s autoplay (game loop
      never ran)
    - errors > 0 in summary (GDScript runtime exception)
    - screenshot file does not exist on disk

DIVISION OF LABOR (4 personas):
  @designer:
    - lock visual theme (one sentence: "robots in steel arena")
    - lock mechanic constants — jump speed, gravity, ball mass,
      spike multiplier, AI reaction radius — BEFORE @operator codes
    - audit final implementation against the constants
    - run autoplay verification (read JSON + screenshot)
    - [RATIFY→@alice intent=close] only when game loop actually ran
  @operator:
    - implement all .gd + .tscn + project polish
    - run Godot batch + autoplay
    - post [EVIDENCE→@reviewer,@designer] with multiple ARTIFACT
      headers (one per deliverable: project.godot, scenes/Main.tscn,
      scripts/*.gd, autoplay-summary.json, screenshots/*.png)
    - rev 9 / D11: STACK MULTIPLE `ARTIFACT:` headers consecutively
      at the start of an evidence body — bridge attaches each row.
      The [KIND] prefix MUST be position-0 (rev 9 / D10) — anything
      before makes parser fall back to plain CLAIM and your
      structured intent is lost.
  @reviewer:
    - read .gd files + autoplay JSON + screenshot
    - check code quality (null safety, signal management, idle vs
      physics frame placement, tree.quit() called)
    - [OBJECT→@operator] on concrete fixes; otherwise ratify
  @investigator:
    - clarify only if @designer skipped a value @operator would
      otherwise have to invent

Phase 7+ closure rules apply (rev 9):
  close_policy=quorum, min_ratifiers=2, requires_artifact=true.
  Bridge has universal carve-outs for evidence/object/defer.
  Use `payload.intent="close"` on the final [RATIFY] OR reply
  to a deliverable (ratifies on artifact-bearing events count
  automatically).

Goal: a runnable volleyball game with original-fidelity mechanics
but visibly different from Pikachu. Don't drag past round 60.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'task',
    'title': 'Godot volleyball clone (different design, same mechanics)',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'designer', 'reviewer', 'investigator'],
    'objective': 'replicate Pikachu Volleyball gameplay in Godot 4.6 with different visual theme',
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'requires_artifact': True,
        'max_rounds': 60,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "  open failed"; exit 1; }
note "  op=$OP policy: quorum=2, requires_artifact=true, max_rounds=60"

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
LAST_CLOSE_REASON=""
LAST_CLOSE_REASON_COUNT=0

note "=== monitor — budget ${WALL_CLOCK_BUDGET_S}s ==="
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "$ELAPSED" -ge "$WALL_CLOCK_BUDGET_S" ]; then
        note "  budget exhausted at ${ELAPSED}s — alice forces close (abandoned)"
        break
    fi

    EVIDENCE=$(count_kind "$OP" chat.speech.evidence)
    PROPOSE=$(count_kind "$OP" chat.speech.propose)
    OBJECT=$(count_kind "$OP" chat.speech.object)
    RATIFIERS=$(distinct_ratifiers "$OP")
    ARTIFACTS=$(artifact_count "$OP")
    SUMMARY_EXISTS=0
    [ -f "$PROJECT_CWD/autoplay-summary.json" ] && SUMMARY_EXISTS=1
    SHOTS=$(ls "$PROJECT_CWD/screenshots/"*.png 2>/dev/null | wc -l | tr -d ' ')
    GD_COUNT=$(ls "$PROJECT_CWD/scripts/"*.gd 2>/dev/null | wc -l | tr -d ' ')
    note "  t=${ELAPSED}s ev=$EVIDENCE prop=$PROPOSE obj=$OBJECT ratifiers=$RATIFIERS artifacts=$ARTIFACTS summary=$SUMMARY_EXISTS shots=$SHOTS gd=$GD_COUNT"

    if [ "$ARTIFACTS" -ge 2 ] && [ "$SUMMARY_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  autoplay summary present + artifacts>=2 → alice ratifies (intent=close)"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice: autoplay summary present, ratifying.","intent":"close"}
        }')
        if [ "$STATUS" = "201" ]; then
            ALICE_RATIFIED=1
        fi
    fi

    if [ "$RATIFIERS" -ge 2 ]; then
        STATUS=$(curl_post -o /tmp/_c -w "%{http_code}" \
            "$BASE/v2/operations/$OP/close" -d '{
                "actor_handle":"@alice","resolution":"completed",
                "summary":"Godot volleyball shipped: GDScript + scene + autoplay summary + screenshots, quorum-ratified"
            }')
        if [ "$STATUS" = "200" ]; then
            note "  close OK"
            break
        fi
        # P10.x: backoff on repeated same-reason close failures.
        REASON=$(cat /tmp/_c 2>/dev/null | head -c 200)
        if [ "$REASON" = "$LAST_CLOSE_REASON" ]; then
            LAST_CLOSE_REASON_COUNT=$((LAST_CLOSE_REASON_COUNT + 1))
        else
            LAST_CLOSE_REASON="$REASON"
            LAST_CLOSE_REASON_COUNT=1
            note "  close HTTP=$STATUS body=$REASON"
        fi
        if [ "$LAST_CLOSE_REASON_COUNT" -le 1 ]; then
            sleep $PROBE_INTERVAL_S
        else
            sleep 60
        fi
        continue
    fi

    sleep $PROBE_INTERVAL_S
done

# Force close: P10.4 — abandoned bypasses requires_artifact.
STATE=$(curl_q "$BASE/v2/operations/$OP" | python -c "import sys, json; print(json.load(sys.stdin).get('state',''))")
if [ "$STATE" != "closed" ]; then
    note "  force close (state=$STATE) as abandoned"
    curl_post "$BASE/v2/operations/$OP/close" -d '{
        "actor_handle":"@alice","resolution":"abandoned",
        "summary":"wall-clock budget exhausted before delivery"
    }' >/dev/null || true
fi

note ""
note "=== artifacts attached ==="
artifacts_json "$OP" | python -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('artifacts', []):
    print(f'  artifact id={a[\"id\"][:8]} kind={a[\"kind\"]:<10} size={a[\"size_bytes\"]:>9} sha={a[\"sha256\"][:12]}.. uri={a[\"uri\"]}')"

note ""
note "=== files on disk ==="
ls -la "$PROJECT_CWD/" 2>/dev/null | head -8
echo
echo "Scripts:"
ls "$PROJECT_CWD/scripts/" 2>/dev/null
echo
echo "Scenes:"
ls "$PROJECT_CWD/scenes/" 2>/dev/null
echo
echo "Screenshots:"
ls "$PROJECT_CWD/screenshots/" 2>/dev/null
echo
echo "Autoplay summary:"
if [ -f "$PROJECT_CWD/autoplay-summary.json" ]; then
    python -c "
import json
d = json.load(open(r'$PROJECT_CWD/autoplay-summary.json'))
print(f'  status={d.get(\"status\")}, reason={d.get(\"failureReason\") or d.get(\"reason\")}')
print(f'  score_p1={d.get(\"score_p1\") or d.get(\"playerScore\")} score_p2={d.get(\"score_p2\") or d.get(\"aiScore\")}')
print(f'  duration={d.get(\"realSeconds\") or d.get(\"duration\") or \"?\"}')
print(f'  errors={d.get(\"errorCount\") or d.get(\"errors\")} warnings={d.get(\"warningCount\") or d.get(\"warnings\")}')"
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
