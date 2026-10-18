"""3D studio at scale: stateless MCP server, several copies behind the "studio" service.

Same tools as in module s3d01 (submit_render, render_status, my_renders): for Open WebUI, nothing
changes. No copy keeps any state in memory: requests go to Redis, and the workers (worker.py)
fetch them there. Each copy also watches for requests whose worker disappeared, and puts them
back in the queue.
"""
import asyncio
import hmac
import time

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import common as c

r = c.connection()
# Stateless: each call stands on its own, any copy can answer it.
mcp = FastMCP("studio", host="0.0.0.0", port=8000, stateless_http=True)


def _headers(ctx: Context):
    req = ctx.request_context.request
    return req.headers if req is not None else {}


def _user(ctx: Context) -> str:
    """The user's address (header filled in by Open WebUI), after checking the studio token."""
    headers = _headers(ctx)
    if headers.get("authorization", "") != f"Bearer {c.TOKEN}":
        raise PermissionError("invalid studio token")
    email = headers.get("x-user-email", "").strip().lower()
    if not email:
        raise PermissionError("unknown user: X-User-Email header missing")
    return email


def _position(number: int) -> int | None:
    """1 = next in line; None if the request is no longer waiting."""
    waiting = [int(x) for x in r.lrange(c.QUEUE, 0, -1)]  # left = most recent
    if number not in waiting:
        return None
    return len(waiting) - waiting.index(number)


# ---------------------------------------------------------------- MCP tools

@mcp.tool()
def submit_render(title: str, code: str, ctx: Context) -> dict:
    """Queues the render of a Blender scene and answers immediately.

    code: Python script (bpy) that builds the scene in an empty scene: objects, materials,
    light, camera. Do not set the render engine or save: the studio does it
    (Cycles, 1280 x 720, PNG + GLB 3D model). Decor objects (floor, table, background) must have
    a name starting with "Decor": they are rendered but not exported in the 3D model.
    Returns the request number and its position in the queue."""
    user = _user(ctx)
    uid = _headers(ctx).get("x-user-id", "").strip()  # notifications (next series)
    number = r.incr("studio:counter")  # atomic: a unique number, even with several copies
    r.hset(c.request(number), mapping={
        "number": number, "title": title[:80], "code": code, "user": user,
        "user_id": uid, "status": "queued", "submitted": time.time(),
    })
    r.lpush(c.QUEUE, number)  # the queue: workers take from the right
    r.lpush(c.LOAD, number)   # the load, read by KEDA: one entry per request waiting or running
    follow_up = "The user will be notified in Open WebUI." if uid else "Ask for its status with render_status."
    return {"number": number, "title": title, "position": _position(number), "message": f"Request recorded. {follow_up}"}


@mcp.tool()
def render_status(number: int, ctx: Context) -> dict:
    """Gives the status of one of the user's requests: queued (with its position), running,
    done (with the image in Markdown and the 3D artifact to copy as is) or error."""
    user = _user(ctx)
    d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(number)).items()}
    if not d or d.get("user") != user:
        return {"number": number, "error": "no request with this number for this user"}
    status = {"number": number, "title": d["title"], "status": d["status"]}
    if d["status"] == "queued":
        status["position"] = _position(number)
    elif d["status"] == "running":
        status["running_seconds"] = round(time.time() - float(d["started"]))
        status["worker"] = d.get("worker", "")
    elif d["status"] == "done":
        status["duration_seconds"] = round(float(d["finished"]) - float(d["started"]))
        status["worker"] = d.get("worker", "")
        status["image_markdown"] = f'![{d["title"]}]({c.link(number, "png")})'
        status["model_3d"] = c.artifact(number, d["title"])
    else:
        status["error"] = d.get("error", "")
    return status


@mcp.tool()
def my_renders(ctx: Context) -> list[dict]:
    """Lists the signed-in user's requests (status, worker, position), most recent first."""
    user = _user(ctx)
    last = int(r.get("studio:counter") or 0)
    results = []
    for n in range(last, 0, -1):
        d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(n)).items()}
        if d.get("user") == user:
            line = {"number": n, "title": d["title"], "status": d["status"]}
            if d["status"] == "queued":
                line["position"] = _position(n)
            if d.get("worker"):
                line["worker"] = d["worker"]
            results.append(line)
    return results


# ---------------------------------------------------------------- Files (signed links) and health

@mcp.custom_route("/r/{number}/{name}", methods=["GET"])
async def serve(request: Request) -> Response:
    try:
        number = int(request.path_params["number"])
        sig, extension = request.path_params["name"].rsplit(".", 1)
    except ValueError:
        return Response(status_code=404)
    if extension not in ("png", "glb") or not hmac.compare_digest(sig, c.signature(number, extension)):
        return Response(status_code=404)
    content = r.get(c.file(number, extension))
    if content is None:
        return Response(status_code=404)
    return Response(content, media_type="image/png" if extension == "png" else "model/gltf-binary",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=3600"})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"ok": bool(r.ping()), "queued": r.llen(c.QUEUE), "processing": r.llen(c.PROCESSING), "load": r.llen(c.LOAD)})


# Routes of the next series (tracking widget, Pipe function): a separate file, not in this repository.
try:
    import integrations
    integrations.connect(mcp, r, _position)
except ImportError:
    pass


# ---------------------------------------------------------------- Orphan requests

async def _watch() -> None:
    """Puts back in the queue the requests whose worker no longer beats (pod deleted, hard stop)."""
    suspects: set[int] = set()  # requests without a heartbeat in the previous round
    while True:
        await asyncio.sleep(3)
        try:
            for raw in r.lrange(c.PROCESSING, 0, -1):
                n = int(raw)
                if r.exists(c.alive(n)):  # the worker beats: all good
                    suspects.discard(n)
                    continue
                if n not in suspects:      # first round without a heartbeat: wait for the next one
                    suspects.add(n)
                    continue
                suspects.discard(n)
                if r.lrem(c.PROCESSING, 1, raw):  # only one server copy wins
                    r.hset(c.request(n), mapping={"status": "queued", "worker": ""})
                    r.rpush(c.QUEUE, n)  # on the right: it goes first again
        except Exception as e:  # Redis briefly unavailable: try again
            print(f"watch: {e}")


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    startup = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(application):
        task = asyncio.create_task(_watch())
        async with startup(application):
            yield
        task.cancel()

    app.router.lifespan_context = lifespan
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
