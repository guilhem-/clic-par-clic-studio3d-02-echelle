#!/usr/bin/env bash
# Envoie quatre demandes au studio, au nom de deux utilisateurs, comme Open WebUI le ferait
# (jeton du studio + en-tête d'identité), puis affiche l'état de la file.
# Prérequis, dans un autre terminal : kubectl -n studio3d port-forward svc/studio 8000:8000
# et le jeton dans l'environnement : export STUDIO_JETON=…
set -euo pipefail
URL=${STUDIO_URL:-http://127.0.0.1:8000}
: "${STUDIO_JETON:?STUDIO_JETON manquant}"
SCENE='import bpy\nbpy.ops.mesh.primitive_plane_add(size=6); bpy.context.object.name = \"Decor sol\"\nbpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1, location=(0, 0, 0.5))'

soumettre() { # $1 : adresse de l'utilisateur, $2 : titre
  curl -s "$URL/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $STUDIO_JETON" -H "X-Utilisateur-Email: $1" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"soumettre_rendu\",\"arguments\":{\"titre\":\"$2\",\"code\":\"$SCENE\"}}}" \
    | sed -n 's/^data: //p'
  echo
}

soumettre lea@example.com "Vase vert" &
soumettre hugo@example.com "Lampe noire" &
soumettre lea@example.com "Vase jaune" &
soumettre hugo@example.com "Lampe blanche" &
wait
echo "File : $(curl -s "$URL/sante")"
echo "Suivez les workers : kubectl -n studio3d get pods -l app=studio-travailleur -w"
