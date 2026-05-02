#!/usr/bin/env bash
# Drive an inquiry against the live NAS bridge and verify EchoBrain
# auto-replies through the broker fan-out.
set -uo pipefail
TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

echo "=== open inquiry addressed to @bridge-agent ==="
OP=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{\"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"agent live smoke\",\"addressed_to\":\"bridge-agent\",\"opener_actor_handle\":\"@alice\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "op_id=$OP"

echo
echo "=== alice asks question (this triggers broker -> agent) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.question\",\"payload\":{\"text\":\"are you there?\"},\"addressed_to\":\"bridge-agent\"}" >/dev/null

echo "=== wait up to 5s for agent to reply ==="
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.5
  KINDS=$(curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "import sys,json; d=json.load(sys.stdin); print(','.join(e['kind'] for e in d['events']))")
  if echo "$KINDS" | grep -q 'chat.speech.claim'; then
    echo "  reply detected after $i ticks"
    break
  fi
done

echo
echo "=== final events timeline ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d['events']:
    text = (e.get('payload') or {}).get('text', '')[:80]
    print(f'  seq={e[\"seq\"]:>2} kind={e[\"kind\"]:<32} text={text!r}')"

echo
echo "=== agent metrics ==="
curl -sk "${H[@]}" "$BASE/v2/diagnostics" | python -c "
import sys, json
d = json.load(sys.stdin)
for a in d['agents']:
    print(f'  agent {a[\"actor_handle\"]}:')
    for k, v in sorted(a['metrics'].items()):
        print(f'    {k:30}= {v}')"
