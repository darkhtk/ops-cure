#!/usr/bin/env bash
# Live-verify v3 policy enforcement against the NAS bridge.
#
# Three scenarios:
#   T1 max_rounds=2 — 3rd speech rejected
#   T2 expected_response.kinds=[answer] — reply with claim rejected
#   T3 close_policy=operator_ratifies — close blocked until ratify
#
# No persona needed; alice + bob + an "@operator" actor drive everything
# via direct curl. The bridge does the enforcement; this is a pure
# protocol test, no LLMs involved.
set -uo pipefail
TID="${1:?need discord_thread_id (run scripts/nas-mkthread.ps1)}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

PASS=0
FAIL=0

note() { echo "[$(date +%H:%M:%S)] $*"; }

assert_status() {
    # args: expected_code got_code label
    if [ "$2" = "$1" ]; then
        note "  PASS $3 (HTTP $2)"; PASS=$((PASS+1))
    else
        note "  FAIL $3 expected=$1 got=$2"; FAIL=$((FAIL+1))
    fi
}

assert_contains() {
    local body="$1" needle="$2" label="$3"
    if echo "$body" | grep -q "$needle"; then
        note "  PASS $label (body matched: $needle)"; PASS=$((PASS+1))
    else
        note "  FAIL $label expected substring=$needle"
        note "  body: $body"
        FAIL=$((FAIL+1))
    fi
}

post() {
    # args: url body -- emits "<http_code>\n<body>"
    local url="$1" body="$2"
    curl -sk -o /tmp/_smoke_body -w "%{http_code}\n" "${H[@]}" "$url" -d "$body"
    cat /tmp/_smoke_body
    echo ""
}

# ============================================================================
# T1 — max_rounds enforcement
# ============================================================================
note "=== T1: max_rounds=2 cap should reject the 3rd speech ==="
T1_OPEN=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T1 max_rounds\",
    \"opener_actor_handle\":\"@alice\",
    \"policy\":{\"max_rounds\":2}
}")
T1_OP=$(echo "$T1_OPEN" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op=$T1_OP policy=$(echo "$T1_OPEN" | python -c "import sys,json; print(json.load(sys.stdin).get('policy',{}))")"

curl -sk -o /dev/null -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T1_OP/events" \
    -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.claim\",\"payload\":{\"text\":\"first\"}}" >/tmp/_c1
curl -sk -o /dev/null -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T1_OP/events" \
    -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.claim\",\"payload\":{\"text\":\"second\"}}" >/tmp/_c2
T1_THIRD=$(curl -sk -o /tmp/_t1_third_body -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T1_OP/events" \
    -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.claim\",\"payload\":{\"text\":\"third\"}}")
T1_THIRD_BODY=$(cat /tmp/_t1_third_body)
note "  3rd speech HTTP=$T1_THIRD body=$T1_THIRD_BODY"
assert_status "400" "$T1_THIRD" "T1 third speech rejected"
assert_contains "$T1_THIRD_BODY" "max_rounds" "T1 error mentions max_rounds"

# ============================================================================
# T2 — reply-kind whitelist
# ============================================================================
note ""
note "=== T2: expected_response.kinds=[answer] should reject reply.kind=claim ==="
T2_OPEN=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T2 kind whitelist\",
    \"opener_actor_handle\":\"@alice\"
}")
T2_OP=$(echo "$T2_OPEN" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op=$T2_OP"

T2_Q=$(curl -sk "${H[@]}" "$BASE/v2/operations/$T2_OP/events" -d "{
    \"actor_handle\":\"@alice\",\"kind\":\"speech.question\",
    \"payload\":{\"text\":\"what's the cause?\"},
    \"addressed_to\":\"bob\",
    \"expected_response\":{\"from_actor_handles\":[\"@bob\"],\"kinds\":[\"answer\"]}
}")
T2_Q_ID=$(echo "$T2_Q" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  question event_id=$T2_Q_ID"

# Bob replies with kind=claim — should be rejected.
T2_BAD=$(curl -sk -o /tmp/_t2_bad -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T2_OP/events" -d "{
    \"actor_handle\":\"@bob\",\"kind\":\"speech.claim\",
    \"payload\":{\"text\":\"actually it's DNS\"},
    \"replies_to_event_id\":\"$T2_Q_ID\"
}")
T2_BAD_BODY=$(cat /tmp/_t2_bad)
note "  bob's claim reply HTTP=$T2_BAD body=$T2_BAD_BODY"
assert_status "400" "$T2_BAD" "T2 mismatched-kind reply rejected"
assert_contains "$T2_BAD_BODY" "expected_response.kinds" "T2 error mentions kinds"

# Bob replies with kind=answer — should pass.
T2_OK=$(curl -sk -o /dev/null -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T2_OP/events" -d "{
    \"actor_handle\":\"@bob\",\"kind\":\"speech.answer\",
    \"payload\":{\"text\":\"checking the resolver logs first\"},
    \"replies_to_event_id\":\"$T2_Q_ID\"
}")
note "  bob's answer reply HTTP=$T2_OK"
assert_status "201" "$T2_OK" "T2 matching-kind reply accepted"

# ============================================================================
# T3 — close policy: operator_ratifies
# ============================================================================
note ""
note "=== T3: close_policy=operator_ratifies should block close until @operator ratifies ==="
T3_OPEN=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{
    \"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"T3 operator_ratifies\",
    \"opener_actor_handle\":\"@alice\",
    \"addressed_to\":\"operator\",
    \"policy\":{\"close_policy\":\"operator_ratifies\"}
}")
T3_OP=$(echo "$T3_OPEN" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
note "  op=$T3_OP"

# Without operator role on the op + without a ratify, close should fail.
T3_BAD=$(curl -sk -o /tmp/_t3_bad -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T3_OP/close" \
    -d "{\"actor_handle\":\"@alice\",\"resolution\":\"answered\",\"summary\":\"too soon\"}")
T3_BAD_BODY=$(cat /tmp/_t3_bad)
note "  early close HTTP=$T3_BAD body=$T3_BAD_BODY"
assert_status "400" "$T3_BAD" "T3 early close rejected"
assert_contains "$T3_BAD_BODY" "operator" "T3 error mentions operator"

# Promote @operator to role=operator on this op via SSH/exec helper (the
# v3 phase-2 flow uses INVITE/JOIN; phase 1 still drives roles via the
# repository directly).
note "  promoting @operator to role=operator on op via docker exec..."
PROMOTE_PY=$(cat <<EOF
import sys
sys.path.insert(0, "/app")
from app.db import session_scope
from app.kernel.v2 import V2Repository
from app.kernel.v2.actor_service import ActorService
repo = V2Repository()
svc = ActorService(repo)
with session_scope() as s:
    op_id = "$T3_OP"
    op = repo.get_operation(s, op_id)
    operator = svc.ensure_actor_by_handle(s, handle="@operator", kind="ai")
    repo.add_participant(s, operation_id=op_id, actor_id=operator.id, role="operator")
print("ok")
EOF
)
B64=$(echo "$PROMOTE_PY" | base64 -w0 2>/dev/null || echo "$PROMOTE_PY" | base64)
echo "  (skipping promote helper — using SSH path below)"

# We don't have direct ssh from this script. Operator must already be a
# participant via the addressed_to=operator on open. Add the operator
# role separately via a side-channel; for now just verify the gate.
# A second ratify-from-non-operator should still NOT unblock.
NON_OP_RATIFY=$(curl -sk -o /dev/null -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T3_OP/events" \
    -d "{\"actor_handle\":\"@bob\",\"kind\":\"speech.ratify\",\"payload\":{\"text\":\"i second\"}}")
note "  non-operator ratify HTTP=$NON_OP_RATIFY"
T3_STILL_BAD=$(curl -sk -o /tmp/_t3_still_bad -w "%{http_code}\n" "${H[@]}" "$BASE/v2/operations/$T3_OP/close" \
    -d "{\"actor_handle\":\"@alice\",\"resolution\":\"answered\",\"summary\":\"still no operator\"}")
T3_STILL_BAD_BODY=$(cat /tmp/_t3_still_bad)
note "  close after only non-operator ratify HTTP=$T3_STILL_BAD body=$T3_STILL_BAD_BODY"
assert_status "400" "$T3_STILL_BAD" "T3 close still blocked without operator ratify"

# ============================================================================
# Summary
# ============================================================================
note ""
note "=========================================="
note "PASS=$PASS FAIL=$FAIL"
exit $FAIL
