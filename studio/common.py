"""3D studio at scale: what the MCP server (api.py) and the workers (worker.py) share.

All the state lives in Redis, so that the server runs as several stateless copies and the
workers fetch the requests themselves:

  studio:counter               number of the next request (INCR)
  studio:request:<n>           hash: title, code, user, user_id, status, submitted, started, finished, error, worker
  studio:queue                 requests waiting (LPUSH on the left, taken from the right)
  studio:processing            requests taken by a worker (BLMOVE queue -> processing)
  studio:load                  one entry per request waiting or running: what KEDA measures
  studio:alive:<n>             heartbeat of the worker handling <n> (expires in 10 s)
  studio:file:<n>:<ext>        PNG and GLB produced (expire in 24 h; in production: object storage)
  studio:notifications         finished renders to announce (next series, read by a notifier)
"""
import hashlib
import hmac
import os

import redis

TOKEN = os.environ.get("STUDIO_TOKEN", "")
SECRET = os.environ.get("STUDIO_SECRET", TOKEN).encode()
PUBLIC = os.environ.get("STUDIO_PUBLIC_URL", "http://studio.localhost:8080").rstrip("/")
FILE_TTL = int(os.environ.get("FILE_TTL", str(24 * 3600)))

QUEUE, PROCESSING, LOAD, NOTIFICATIONS = "studio:queue", "studio:processing", "studio:load", "studio:notifications"
request = lambda n: f"studio:request:{n}"
alive = lambda n: f"studio:alive:{n}"
file = lambda n, ext: f"studio:file:{n}:{ext}"


def connection() -> redis.Redis:
    return redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379,
                       password=os.environ.get("REDIS_PASSWORD") or None, decode_responses=False)


def text(value) -> str:
    return value.decode() if isinstance(value, bytes) else (value or "")


def signature(number: int, extension: str) -> str:
    return hmac.new(SECRET, f"{number}.{extension}".encode(), hashlib.sha256).hexdigest()[:10]


def link(number: int, extension: str) -> str:
    return f"{PUBLIC}/r/{number}/{signature(number, extension)}.{extension}"


def artifact(number: int, title: str) -> str:
    return (
        '```html\n'
        '<script type="module" src="https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js"></script>\n'
        f'<model-viewer src="{link(number, "glb")}" poster="{link(number, "png")}" alt="{title}" '
        'camera-controls auto-rotate shadow-intensity="1" '
        'style="width:100%;height:480px;background:#f4f4f5"></model-viewer>\n'
        '```'
    )
