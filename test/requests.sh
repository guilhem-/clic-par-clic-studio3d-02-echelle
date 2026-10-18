#!/usr/bin/env bash
# Sends four requests to the studio, on behalf of two users, as Open WebUI would
# (studio token + identity header), then shows the state of the queue.
# Prerequisites, in another terminal: kubectl -n studio3d port-forward svc/studio 8000:8000
# and the token in the environment: export STUDIO_TOKEN=…
set -euo pipefail
URL=${STUDIO_URL:-http://127.0.0.1:8000}
: "${STUDIO_TOKEN:?STUDIO_TOKEN missing}"
SCENE='import bpy\nbpy.ops.mesh.primitive_plane_add(size=6); bpy.context.object.name = \"Decor floor\"\nbpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1, location=(0, 0, 0.5))'

submit() { # $1: the user's address, $2: title
  curl -s "$URL/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $STUDIO_TOKEN" -H "X-User-Email: $1" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"submit_render\",\"arguments\":{\"title\":\"$2\",\"code\":\"$SCENE\"}}}" \
    | sed -n 's/^data: //p'
  echo
}

submit alex@example.com "Green vase" &
submit sam@example.com "Black lamp" &
submit alex@example.com "Yellow vase" &
submit sam@example.com "White lamp" &
wait
echo "Queue: $(curl -s "$URL/health")"
echo "Follow the workers: kubectl -n studio3d get pods -l app=studio-worker -w"
