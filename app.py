"""The lego app: a local web page for running one agent task at a time and watching the door.

    lego app [PORT]          with your policy
    lego app --demo [PORT]   with synthetic private data on gpu-box, deleted when the app stops

It serves app.html and a small JSON API on 127.0.0.1 only. Each task is one unattended
agent session in its VM (agents.ONESHOT); the page polls the door's log, the ledger and
the route's capture while it runs.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agents
import door
import lego

PAGE = Path(__file__).resolve().parent / "app.html"


class App:
    def __init__(self, policy, state):
        self.p, self.state = policy, Path(state)
        self.door = door.Door(policy, state)                 # for labels and the ledger file
        self.runs, self.lock = [], threading.Lock()

    def info(self):
        return {"workspace": self.p["workspace"], "agents": list(agents.AGENTS), "deadline": self.p["deadline"],
                "allowance": self.p.get("allowance", {}), "ledger": json.loads(self.door.ledger_file.read_text()),
                "catalog": [{"ref": r, "scopes": sorted(self.door.item(r).scopes)} for r in self.p.get("catalog", [])],
                "public": self.p.get("public", []), "nodes": {n: {"trust": v["trust"], "model": (v.get("model") or {})
                                                                  .get("model")} for n, v in self.p["nodes"].items()},
                "busy": any(r["status"] == "running" for r in self.runs), "runs": [self.view(r, False) for r in self.runs]}

    def start(self, agent, prompt):
        with self.lock:
            if any(r["status"] == "running" for r in self.runs):
                return None
            log = self.state / "door.jsonl"
            run = {"id": len(self.runs), "agent": agent, "prompt": prompt, "status": "running", "t0": time.time(),
                   "log_start": log.stat().st_size if log.exists() else 0, "reply": "", "seconds": None,
                   "capture": str(self.state / f"route-{len(self.runs)}.jsonl")}
            self.runs.append(run)
        threading.Thread(target=self.work, args=(run,), daemon=True).start()
        return run["id"]

    def work(self, run):
        args, reply = agents.ONESHOT[run["agent"]]
        try:
            r = lego.session(self.p, self.state, run["agent"], args(run["prompt"]), capture=run["capture"], timeout=1800)
            try:
                run["reply"] = reply(r.stdout) or r.stderr[-2000:]
            except (ValueError, KeyError):
                run["reply"] = (r.stdout + r.stderr)[-4000:]
            run["status"] = "done" if r.returncode == 0 else f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            run["status"] = "timed out"
        except Exception as e:                                # shown on the page, not hidden
            run["status"], run["reply"] = "error", f"{type(e).__name__}: {e}"
        run["seconds"] = round(time.time() - run["t0"], 1)

    def view(self, run, full=True):
        log = self.state / "door.jsonl"
        raw = log.read_bytes()[run["log_start"]:].decode(errors="replace") if log.exists() else ""
        events = [e for e in map(json.loads, raw.splitlines()) if e["t"] >= run["t0"] - 1]
        if run["seconds"] is not None:
            events = [e for e in events if e["t"] <= run["t0"] + run["seconds"] + 1]
        cap = Path(run["capture"])
        calls = sum(1 for _ in cap.open()) if cap.exists() else 0
        out = {k: run[k] for k in ("id", "agent", "prompt", "status", "seconds")}
        out.update(elapsed=round(time.time() - run["t0"], 1) if run["seconds"] is None else run["seconds"],
                   offloads=sum(e["op"] == "offload" for e in events), refused=sum(e["op"] == "refused" for e in events),
                   route_calls=calls, route_bytes=cap.stat().st_size if cap.exists() else 0)
        if full:
            out.update(events=events, reply=run["reply"])
        return out


def serve(app, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/info":
                return self.send(200, app.info())
            if self.path.startswith("/api/run/"):
                i = int(self.path.rsplit("/", 1)[1])
                return self.send(200, app.view(app.runs[i])) if 0 <= i < len(app.runs) else self.send(404, {})
            self.send(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self.send(403, {"error": "cross-origin request"})
            if self.path == "/api/run":
                if body.get("agent") not in agents.AGENTS or not str(body.get("prompt", "")).strip():
                    return self.send(400, {"error": "pick an agent and write a task"})
                rid = app.start(body["agent"], body["prompt"].strip())
                return self.send(409, {"error": "a task is already running"}) if rid is None else self.send(200, {"id": rid})
            if self.path == "/api/reset":
                app.door.reset()
                return self.send(200, {"ok": True})
            self.send(404, {})

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main(policy, state, port):
    srv = serve(App(policy, state), port)
    url = f"http://127.0.0.1:{port}/"
    print(f"lego app on {url}  (Ctrl-C to stop)", flush=True)
    if not os.environ.get("LEGO_NO_OPEN"):
        webbrowser.open(url)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):    # any stop runs the cleanup below
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def demo(port):
    """The e2e test's synthetic private data and policy, in a folder deleted when the app stops."""
    import shutil
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
    import e2e
    root = Path(tempfile.mkdtemp(prefix="lego-demo-"))
    os.environ["LEGO_TAINT"] = str(root / "taint.json")
    simple, _ = e2e.make(root)
    policy = door.expand(simple, root / ".lego")
    policy["workspace"] = door.canon(policy["workspace"])
    print(f"demo data: {root / 'private'}\nworkspace: {policy['workspace']}", flush=True)
    try:
        main(policy, root / ".lego", port)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        print("demo data deleted", flush=True)
