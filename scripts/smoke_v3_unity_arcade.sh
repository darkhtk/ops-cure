#!/usr/bin/env bash
# Unity arcade game build — 4 personas develop a 1-hour-runtime
# arcade game with batch build + headless autoplay verification.
#
# Architecture (no Unity Editor needed):
#   - Agents write Unity .cs scripts via filesystem in cwd (UnityArcade)
#   - An Editor build script compiles via Unity batch mode
#     (Unity.exe -batchmode -nographics -executeMethod BuildScript.Build)
#   - The built executable runs with -automation flags
#   - AutomationTestRunner ticks the agent's IAutomationDriver,
#     captures screenshots on schedule, writes autoplay-summary.json
#   - Agents read the JSON + screenshots back, ratify or object
#
# Phase 7 closure (unchanged):
#   - kind=task, bind_remote_task=false default (T1.1)
#   - close_policy=quorum, min_ratifiers=2
#   - requires_artifact=true (T2.1)
#   - operator's [EVIDENCE] uses ARTIFACT header (T1.2) on each
#     deliverable (.cs scripts, the built .exe, screenshots, .json summary)
set -uo pipefail

TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H_AUTH="Authorization: Bearer $TOK"
H_JSON="Content-Type: application/json"

PROJECT_CWD="C:/Users/darkh/Projects/ops-cure-scratch/UnityArcade"
WALL_CLOCK_BUDGET_S=5400   # 90 minutes
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

note "=== open op (kind=task, Unity arcade build) ==="
PROMPT=$(cat <<'EOF'
Project: build a 1-hour-runtime browser-or-Windows arcade game in
the Unity project at your cwd (UnityArcade/). Agents work entirely
via filesystem + Unity batch build — Unity Editor does NOT need to
be open.

Deliverable: a Windows standalone executable at Builds/UnityArcade.exe
that launches into a playable arcade game with at least 1 hour of
varied content (procedurally scaling difficulty, multiple enemy
patterns, score persistence, run-end summary). The exact genre is
your call — pick one that fits arcade pacing (top-down shooter,
auto-survivor, side-scroller, etc.).

INFRASTRUCTURE ALREADY PROVIDED (do not rewrite, USE):
  Assets/Scripts/Runtime/RuntimeLaunchOptions.cs  — parses -automation,
                                                    -automation-duration,
                                                    -automation-out, etc.
  Assets/Scripts/Runtime/RuntimeDevLog.cs         — file logger,
                                                    category counters
  Assets/Scripts/Runtime/AutomationTestRunner.cs  — autoplay tick
                                                    loop, screenshot
                                                    schedule, JSON
                                                    summary writer.
                                                    Plug in your
                                                    IAutomationDriver
                                                    via:
      AutomationTestRunner.Driver = new MyArcadeDriver(...);
      AutomationTestRunner.EnsureInScene();

REFERENCE (study, don't copy):
  C:/Users/darkh/Projects/UlalaCheese/Assets/Scripts/Runtime/Core/
  AutomationTestRunner.cs — full-featured pattern with movement
  intent, card auto-pick, FPS sampling, summary serialization.

YOU OWN:
  1. Game logic (player, enemies, projectiles, score, levels) in
     Assets/Scripts/Runtime/Game/*.cs
  2. IAutomationDriver implementation that hits the player with
     auto-pilot inputs and exposes CurrentScore/Level/Kills/etc.
     so the autoplay run validates the loop functionally
  3. Editor build script Assets/Editor/BuildScript.cs with a
     [MenuItem] + static Build() entry point invokable from
     Unity batch mode:
       Unity.exe -batchmode -nographics -projectPath . \
         -executeMethod Builder.BuildScript.Build -quit -logFile -
  4. A bootstrap scene Assets/Scenes/Main.unity (use Unity's
     scene YAML — handcraft the scene file or have your build
     script create it programmatically; either works)
  5. EditorBuildSettings configured so Main is scene 0
  6. README at root explaining how to play + how autoplay works

AUTOPLAY VERIFICATION (mandatory before close):
  After your build succeeds, run the autoplay harness:
    Builds/UnityArcade.exe -batchmode -nographics \
      -automation -automation-duration 60 \
      -automation-out Builds/autoplay-summary.json \
      -automation-capture 10,30,55
  Expected output:
    - Builds/autoplay-summary.json with status="passed" (or "failed"
      with a concrete reason)
    - 3 PNG screenshots under Builds/screenshots/
    - non-zero score / kills / level (proves the game loop ran)
  Two of you (reviewer + designer, OR designer + investigator)
  must read the JSON + at least one screenshot before [RATIFY].
  Failure modes that count as deviation from "1-hour arcade game":
    - status=failed
    - score=0 AND kills=0 (game loop didn't actually run)
    - errorCount > 5 (compile/runtime errors)
    - avgFps < 20 (game doesn't perform)

DIVISION OF LABOR (4 personas):
  @designer: define scope before code starts. Genre, player stats,
    enemy types, scoring formula, difficulty curve, UI layout. Use
    [PROPOSE→@operator,@alice] to surface decisions; ratify when
    spec is concrete enough to build against. Audit operator's
    implementation for spec drift and run autoplay check against
    the JSON summary's spec-conformance values (HP, score, etc).
  @operator: build it. Write .cs files via your Write tool, run
    Unity batch build via Bash, run autoplay, attach evidence.
    Each major delivery as [EVIDENCE→@reviewer,@designer] with
    ARTIFACT header on the relevant file (.cs / .exe / .json /
    .png). Multiple [EVIDENCE] events expected — scaffold first,
    then game loop, then autoplay run.
  @reviewer: read .cs files + autoplay JSON + screenshots after
    each [EVIDENCE]. Check code quality (Unity API usage, null
    safety, memory leaks) and game-loop sanity. [OBJECT→@operator]
    with concrete fix on issues. Otherwise [RATIFY→@alice].
  @investigator: only when @designer skipped a value @operator
    would otherwise have to invent (rare — designer is usually
    thorough). Otherwise [RATIFY→@alice] once the autoplay JSON
    + screenshots line up with the ratified spec.

Build commands (memorize, use exactly):
  Unity.exe path:
    "C:/Program Files/Unity/Hub/Editor/6000.3.9f1/Editor/Unity.exe"
  Build:
    Unity.exe -batchmode -nographics -projectPath . \
      -executeMethod Builder.BuildScript.Build \
      -quit -logFile build.log
  Inspect log on failure:
    grep -E "error|Build succeeded|Build Player" build.log
  Run autoplay:
    Builds/UnityArcade.exe -batchmode -nographics \
      -automation -automation-duration 60 \
      -automation-out Builds/autoplay-summary.json \
      -automation-capture 10,30,55

Phase 7 closure rules apply (unchanged from RPG smoke):
  close_policy=quorum, min_ratifiers=2, requires_artifact=true.
  Bridge has universal carve-outs for evidence/object/defer (rev 8) —
  patch loops are unblocked. Use kinds=* (or omit) when
  inviting unless explicitly forcing a vote.

Goal: ship a runnable .exe that plays the arcade loop, with
autoplay JSON proving the loop actually executes. Don't drag past
round 50.
EOF
)

OP=$(curl_post "$BASE/v2/operations" -d "$(python -c "
import json, sys
print(json.dumps({
    'space_id': '$TID',
    'kind': 'task',
    'title': 'Unity arcade build (4 personas, autoplay-verified)',
    'opener_actor_handle': '@alice',
    'addressed_to_many': ['operator', 'designer', 'reviewer', 'investigator'],
    'objective': 'build a 1-hour-runtime Unity arcade game with batch build + headless autoplay JSON + screenshots',
    'policy': {
        'close_policy': 'quorum',
        'min_ratifiers': 2,
        'requires_artifact': True,
        'max_rounds': 50,
    },
}))")" | python -c "import sys, json; d = json.load(sys.stdin); print(d.get('id', ''))")
[ -z "$OP" ] && { note "  open failed"; exit 1; }
note "  op=$OP policy: quorum=2, requires_artifact=true, max_rounds=50"

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
    EXE_EXISTS=0
    SUMMARY_EXISTS=0
    [ -f "$PROJECT_CWD/Builds/UnityArcade.exe" ] && EXE_EXISTS=1
    [ -f "$PROJECT_CWD/Builds/autoplay-summary.json" ] && SUMMARY_EXISTS=1
    SHOTS=$(ls "$PROJECT_CWD/Builds/screenshots/"*.png 2>/dev/null | wc -l | tr -d ' ')
    note "  t=${ELAPSED}s ev=$EVIDENCE prop=$PROPOSE obj=$OBJECT ratifiers=$RATIFIERS artifacts=$ARTIFACTS exe=$EXE_EXISTS summary=$SUMMARY_EXISTS shots=$SHOTS"

    # Once an autoplay summary exists AND artifacts ≥ 2 (code + summary), alice ratifies
    if [ "$ARTIFACTS" -ge 2 ] && [ "$SUMMARY_EXISTS" = "1" ] && [ "$ALICE_RATIFIED" = "0" ]; then
        note "  autoplay summary present + artifacts>=2 → alice ratifies"
        STATUS=$(curl_post -o /tmp/_a -w "%{http_code}" "$BASE/v2/operations/$OP/events" -d '{
            "actor_handle":"@alice","kind":"speech.ratify",
            "payload":{"text":"alice: autoplay summary present at Builds/autoplay-summary.json, ratifying."}
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
                "summary":"Unity arcade shipped: build + autoplay-summary + screenshots, quorum-ratified"
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
note "=== artifacts attached ==="
artifacts_json "$OP" | python -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('artifacts', []):
    print(f'  artifact id={a[\"id\"][:8]} kind={a[\"kind\"]:<10} size={a[\"size_bytes\"]:>9} sha={a[\"sha256\"][:12]}.. uri={a[\"uri\"]}')"

note ""
note "=== build artifacts on disk ==="
ls -la "$PROJECT_CWD/Builds/" 2>/dev/null | head -10
echo
echo "Screenshots:"
ls "$PROJECT_CWD/Builds/screenshots/" 2>/dev/null
echo
echo "Autoplay summary status:"
if [ -f "$PROJECT_CWD/Builds/autoplay-summary.json" ]; then
    python -c "
import json
d = json.load(open(r'$PROJECT_CWD/Builds/autoplay-summary.json'))
print(f'  status={d.get(\"status\")}, reason={d.get(\"failureReason\")}')
print(f'  score={d.get(\"score\")} kills={d.get(\"kills\")} level={d.get(\"level\")}')
print(f'  errors={d.get(\"errorCount\")} warnings={d.get(\"warningCount\")} avgFps={d.get(\"avgFps\"):.1f}')
print(f'  driver={d.get(\"driverName\")}')"
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
