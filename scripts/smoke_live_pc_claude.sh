#!/usr/bin/env bash
# Drive a question through the live NAS bridge and wait for a real
# PC claude reply to come back via PCClaudeBrain + ReplyWatcher.
set -uo pipefail
TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

echo "=== open inquiry addressed to @bridge-agent ==="
OP=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{\"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"pc-claude live smoke\",\"addressed_to\":\"bridge-agent\",\"opener_actor_handle\":\"@alice\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "op_id=$OP"

echo
echo "=== alice asks a real question ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.question\",\"payload\":{\"text\":\"what is 2 plus 2? answer in one sentence.\"},\"addressed_to\":\"bridge-agent\"}" >/dev/null

echo
echo "=== wait up to 60s for PC claude run to complete + ReplyWatcher to post ==="
TICKS=120
for i in $(seq 1 $TICKS); do
  sleep 0.5
  EVENTS=$(curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice")
  CLAIMS=$(echo "$EVENTS" | python -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for e in d['events'] if e['kind']=='chat.speech.claim'))")
  if [ "$CLAIMS" -gt 0 ]; then
    echo "  reply detected after $i ticks (~$(echo "$i * 0.5" | bc 2>/dev/null || echo "$i*0.5")s)"
    break
  fi
  if [ $((i % 10)) -eq 0 ]; then echo "  ... still waiting (${i}/${TICKS})"; fi
done

echo
echo "=== final timeline ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "
import sys, json
d = json.load(sys.stdin)
for e in d['events']:
    text = (e.get('payload') or {}).get('text', '')[:120]
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
