#!/usr/bin/env bash
# Drive a full v2 behavior cycle against the LIVE NAS bridge.
# Args: $1 = discord_thread_id (already provisioned in the bridge DB)
set -uo pipefail
TID="${1:?need discord_thread_id}"
BASE="${BRIDGE_BASE:-http://172.30.1.12:18080}"
TOK="${BRIDGE_TOK:-kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9}"
H=( -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" )

echo "=== diagnostics (initial) ==="
curl -sk "${H[@]}" "$BASE/v2/diagnostics" | python -m json.tool

echo
echo "=== open inquiry op (alice -> bridge-agent) ==="
OP=$(curl -sk "${H[@]}" "$BASE/v2/operations" -d "{\"space_id\":\"$TID\",\"kind\":\"inquiry\",\"title\":\"live smoke: where are last week's logs?\",\"addressed_to\":\"bridge-agent\",\"opener_actor_handle\":\"@alice\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "op_id=$OP"

echo
echo "=== alice posts question ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@alice\",\"kind\":\"speech.question\",\"payload\":{\"text\":\"where are last week's logs?\"},\"addressed_to\":\"bridge-agent\"}" | python -c "import sys,json; d=json.load(sys.stdin); print('seq=', d.get('seq'), 'kind=', d.get('kind'))"

echo
echo "=== bridge-agent posts a manual reply (no agent runner enabled on NAS) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events" -d "{\"actor_handle\":\"@bridge-agent\",\"kind\":\"speech.claim\",\"payload\":{\"text\":\"smoke reply: rotated to /var/log/archive/2026-04-26.log\"}}" | python -c "import sys,json; d=json.load(sys.stdin); print('seq=', d.get('seq'), 'kind=', d.get('kind'))"

echo
echo "=== events listing (alice's view) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/events?actor_handle=%40alice" | python -c "import sys,json; d=json.load(sys.stdin);
for e in d['events']:
    text = (e.get('payload') or {}).get('text','')[:80]
    print(f'  seq={e[\"seq\"]:>2} kind={e[\"kind\"]:<32} text={text!r}')"

echo
echo "=== close op (alice) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/close" -d "{\"actor_handle\":\"@alice\",\"resolution\":\"answered\",\"summary\":\"live smoke close\"}" | python -c "import sys,json; d=json.load(sys.stdin); print('state=', d.get('state'), 'res=', d.get('resolution'))"

echo
echo "=== artifacts (digest summary should be auto-attached) ==="
curl -sk "${H[@]}" "$BASE/v2/operations/$OP/artifacts" | python -c "import sys,json; d=json.load(sys.stdin);
for a in d['artifacts']:
    print(f'  kind={a[\"kind\"]:<10} mime={a[\"mime\"]:<25} label={a.get(\"label\",\"\")}')"

echo
echo "=== inbox state filter (alice's open ops) ==="
curl -sk "${H[@]}" "$BASE/v2/inbox?actor_handle=%40alice&state=open" | python -m json.tool

echo
echo "=== diagnostics (final) ==="
curl -sk "${H[@]}" "$BASE/v2/diagnostics" | python -m json.tool
