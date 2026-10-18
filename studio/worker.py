"""3D studio at scale: Blender worker (several copies, number driven by KEDA).

Fetches a request from the queue when it is free (BLMOVE queue -> processing), renders it with a
fresh Blender (render.py), stores the files in Redis, then starts again. During the render, it beats
(studio:alive:<n>, every 3 s, expires in 10 s): if it disappears, the server puts the request back in the queue. When asked to stop
(SIGTERM, fewer workers), it finishes its current render, then stops.
"""
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import common as c

BLENDER = os.environ.get("BLENDER", "/opt/blender/blender")
RENDER = Path(__file__).with_name("render.py")
MAX_DURATION = int(os.environ.get("MAX_DURATION", "300"))
NAME = socket.gethostname()  # pod name
r = c.connection()
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())


def beat(number: int, finished: threading.Event) -> None:
    """Heartbeat: while the render lasts, a key that expires in 10 s, renewed every 3 s."""
    while not finished.wait(3):
        r.setex(c.alive(number), 10, NAME)


def render(number: int) -> None:
    d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(number)).items()}
    r.setex(c.alive(number), 10, NAME)
    r.hset(c.request(number), mapping={"status": "running", "started": time.time(), "worker": NAME})
    finished = threading.Event()
    threading.Thread(target=beat, args=(number, finished), daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "scene.py").write_text(d.get("code", ""), encoding="utf-8")
            # The model's code runs in this Blender, without the worker's environment variables.
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp}
            output = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(RENDER), "--", str(folder)],
                                    capture_output=True, text=True, timeout=MAX_DURATION, env=env).stdout
            result = folder / "result.json"
            if not result.exists():
                raise RuntimeError("\n".join(output.strip().splitlines()[-12:]) or "Blender produced nothing")
            info = json.loads(result.read_text(encoding="utf-8"))
            if info.get("error"):
                raise RuntimeError(info["error"])
            r.setex(c.file(number, "png"), c.FILE_TTL, (folder / "render.png").read_bytes())
            r.setex(c.file(number, "glb"), c.FILE_TTL, (folder / "model.glb").read_bytes())
        r.hset(c.request(number), mapping={"status": "done", "finished": time.time()})
    except subprocess.TimeoutExpired:
        r.hset(c.request(number), mapping={"status": "error", "finished": time.time(), "error": f"render interrupted after {MAX_DURATION} s"})
    except Exception as e:  # the message goes back to the model, which can fix and resubmit
        r.hset(c.request(number), mapping={"status": "error", "finished": time.time(), "error": str(e)[-1500:]})
    finally:
        finished.set()
        r.lrem(c.PROCESSING, 1, number)
        r.lrem(c.LOAD, 1, number)  # request finished (successful or not): KEDA may scale down
        r.delete(c.alive(number))
        if d.get("user_id"):  # notifications (next series): a notifier will tell the user
            r.lpush(c.NOTIFICATIONS, number)


def main() -> None:
    print(f"{NAME} ready", flush=True)
    while not stop.is_set():  # stop requested (SIGTERM): finish the current render, then exit
        # Waits 2 s at most for a request, and moves it in one go from the queue to "processing".
        raw = r.blmove(c.QUEUE, c.PROCESSING, 2, "RIGHT", "LEFT")
        if raw is None:
            continue
        number = int(raw)
        print(f"{NAME}: request {number}", flush=True)
        render(number)
    print(f"{NAME} stopped", flush=True)


if __name__ == "__main__":
    main()
