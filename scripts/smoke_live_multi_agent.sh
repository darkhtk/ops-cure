#!/usr/bin/env bash
# External-agent multi-party smoke against the live NAS bridge.
# Two PC executors run as external agents (@bridge-agent, @bridge-reviewer).
# alice asks @bridge-agent a question; both agents are in the op so
# alice can re-address @bridge-reviewer for a second opinion.
set -uo pipefail
TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

echo "=== open op addressed to @bridge-agent ==="
OP=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{\"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"multi-agent smoke\",\"addressed_to\":\"bridge-agent\",\"opener_actor_handle\":\"@alice\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "op_id=$OP"

echo
echo "=== alice asks @bridge-agent: 'what is 2+2?' ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.question\",\"payload\":{\"text\":\"what is 2+2? answer in one sentence.\"},\"addressed_to\":\"bridge-agent\"}" >/dev/null

echo "=== wait for bridge-agent reply ==="
for i in $(seq 1 120); do
  sleep 0.5
  CLAIMS=$(curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for e in d['events'] if e['kind']=='chat.speech.claim'))")
  if [ "$CLAIMS" -gt 0 ]; then echo "  agent A replied after $i ticks"; break; fi
done

echo
echo "=== alice now addresses @bridge-reviewer for a second opinion ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.question\",\"payload\":{\"text\":\"@bridge-reviewer: do you agree with that answer? respond yes or no with one sentence reason.\"},\"addressed_to\":\"bridge-reviewer\"}" >/dev/null

echo "=== wait for bridge-reviewer reply ==="
for i in $(seq 1 120); do
  sleep 0.5
  CLAIMS=$(curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for e in d['events'] if e['kind']=='chat.speech.claim'))")
  if [ "$CLAIMS" -ge 2 ]; then echo "  agent B replied after $i ticks"; break; fi
done

echo
echo "=== final timeline (alice's view) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d['events']:
    text = (e.get('payload') or {}).get('text', '')[:140]
    actor = e.get('actor_id','')[:10]
    print(f'  seq={e[\"seq\"]:>2} actor={actor:<10} kind={e[\"kind\"]:<32} text={text!r}')"
