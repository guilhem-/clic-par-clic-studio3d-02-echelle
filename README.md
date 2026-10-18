# Scaling up the 3D studio — Redis, Blender workers and KEDA

Code for module 2 of the “Click by Click” series **Building an agent-ready AI service: the 3D studio**
(also published in French as « Clic par clic » and in German as « Klick für Klick »).

Video: {{video}}

## Goal

The module 1 studio fitted in a single pod: one Blender, an in-memory queue, lost at the slightest
restart. This repository scales it up **without anything changing for Open WebUI** (same `studio`
service, same MCP tools):

- the **MCP server** runs as two **stateless** copies (`stateless_http=True`): any copy answers,
  none keeps state in memory;
- all the state lives in **Redis**: counter, one hash per request, queue, list of requests in
  progress, load;
- the **Blender workers** fetch the work themselves when they are free (`BLMOVE` queue →
  processing, atomic): no network load balancer, one render at a time per worker;
- a **heartbeat** (Redis key renewed every 3 s, expiring after 10 s): if a worker disappears in the
  middle of a render, the server puts its request back at the head of the queue;
- **KEDA** sets the number of workers from the `studio:load` list (one entry per request waiting
  or running): from zero to four, back to zero when everything is done.

## Contents

| File | Role |
|---|---|
| `studio/common.py` | What the server and the workers share: Redis keys, connection, signed links |
| `studio/api.py` | Stateless MCP server: tools `submit_render`, `render_status`, `my_renders`, signed renders `/r/…`, health check `/health`, watch over orphan requests |
| `studio/worker.py` | Worker: `BLMOVE` loop, render by a fresh headless Blender, heartbeat, clean stop on `SIGTERM` |
| `studio/render.py` | Run by Blender: builds the scene, adds a camera and a light if needed, renders the image, exports the GLB (unchanged since module 1) |
| `studio/Dockerfile` | One image for both roles: Python 3.12, Blender 5.2 (official mirror, SHA-256 checksum verified), Redis client |
| `k8s-scale.yaml` | Redis, MCP server ×2, `studio` service, workers (0 copies to start with), KEDA `ScaledObject`, publishing of the renders, network rules |
| `open-webui/` | Identity header of the MCP connection and system prompt of the “Studio 3D” model |
| `test/requests.sh` | Sends four requests on behalf of two users, as Open WebUI would |
| `.env.example` | Variables to fill in (shared token, Redis password), without any value |

## Prerequisites

- The **module 1** studio and its connection in Open WebUI (repository
  [`clic-par-clic-studio3d-01-studio-mcp`](https://github.com/guilhem-/clic-par-clic-studio3d-01-studio-mcp)):
  this module starts from there. You can also start from scratch by following steps 4 and 5 of the
  module 1 README for the connection and the model.
- Docker, `kubectl` and a Kubernetes cluster. The video uses **k3d** (cluster `tterm`, Traefik
  published on port 8080 of the machine).
- **KEDA 2.21** (installed in step 2).
- About 2 CPUs and 2 GB of memory per worker (four workers at most).

## Step-by-step installation

1. **Image** (the server and the workers share it):

   ```bash
   docker build -t studio3d:2 studio
   k3d image import studio3d:2 -c tterm      # k3d; otherwise, push the image to your registry
   ```

2. **KEDA**, once per cluster:

   ```bash
   kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.21.0/keda-2.21.0.yaml
   kubectl -n keda rollout status deployment/keda-operator
   ```

3. **Secrets.** Copy `.env.example` to `.env`, fill in `STUDIO_TOKEN` (the one from module 1) and
   `REDIS_PASSWORD` (for example `openssl rand -hex 24`), then:

   ```bash
   set -a; . ./.env; set +a
   kubectl create namespace studio3d --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n studio3d create secret generic studio \
     --from-literal=token=$STUDIO_TOKEN --from-literal=redis=$REDIS_PASSWORD \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

4. **From the simple studio to the scaled studio.** Remove the module 1 deployment and network
   rule (the `studio` service is kept as is), then apply the manifest:

   ```bash
   kubectl -n studio3d delete deployment/studio networkpolicy/studio-no-egress --ignore-not-found
   kubectl apply -f k8s-scale.yaml
   kubectl -n studio3d rollout status deployment/studio-api
   ```

   In `k8s-scale.yaml`, adapt the public address of the renders (`STUDIO_PUBLIC_URL`, `Ingress`
   host) to your cluster.

5. **Open WebUI**: nothing to change if the module 1 “Studio 3D” connection exists (same URL
   `http://studio.studio3d.svc.cluster.local:8000/mcp`, same `X-User-Email` header). For the demo of
   the video, the model's system prompt is `open-webui/system-prompt.txt` (it adds the list of
   renders with the worker of each one).

6. **Try it.** In one terminal, follow the workers:

   ```bash
   kubectl -n studio3d get pods -l app=studio-worker -w
   ```

   In another one, open a local access and send requests:

   ```bash
   kubectl -n studio3d port-forward svc/studio 8000:8000 &
   ./test/requests.sh
   ```

   At the same time, ask for two renders from Open WebUI. KEDA starts the workers within seconds.
   To simulate a failure, delete a worker in the middle of a render:

   ```bash
   kubectl -n studio3d delete pod <worker-name> --grace-period=0 --force
   ```

   About fifteen seconds later, its request goes back to “queued”, then is taken over by another
   worker (`curl http://127.0.0.1:8000/health`). About thirty seconds after the last render, the
   workers stop, down to zero.

## Good to know

- **KEDA and several triggers**: KEDA keeps the largest value, not the sum. Hence a single list,
  `studio:load`, which counts the requests waiting **and** running.
- **Stopping a worker**: `terminationGracePeriodSeconds: 120`; on `SIGTERM`, the worker finishes
  its render before stopping. A forced deletion (`--force`) simulates a real failure.
- **Network rules**: the server and the workers only reach Redis (and name resolution); Redis only
  accepts the studio and KEDA. They only apply if your cluster network supports `NetworkPolicy`
  (k3d does).
- **`user_id` and `studio:notifications`**: meant for the next series (notifying the user in
  Open WebUI); without an `X-User-Id` header, they stay unused.

## Security and production

- The MCP endpoint (`/mcp`) is not published outside the cluster; only the renders (`/r/…`) are,
  through signed links (HMAC).
- Limits of this example: Redis also keeps the files (24 h); the code written by the model runs in
  the worker, which can reach Redis. In production: object storage for the files, Blender isolated
  in a container with no network, priorities per user group, GPUs for the renders, secrets in your
  vault.

## Next in the series

- Previous module: **A 3D studio for the whole organization** — repository
  [`clic-par-clic-studio3d-01-studio-mcp`](https://github.com/guilhem-/clic-par-clic-studio3d-01-studio-mcp).
- Next module: **Monitoring the studio with Grafana** (Prometheus metrics, dashboard and alert
  provisioned from files) — repository
  [`clic-par-clic-studio3d-03-grafana`](https://github.com/guilhem-/clic-par-clic-studio3d-03-grafana).

## License

MIT, see [`LICENSE`](LICENSE). Blender is distributed under the GPL license by the Blender Foundation;
Redis and KEDA are independent projects. This series is independent of the publishers of the
software shown.
