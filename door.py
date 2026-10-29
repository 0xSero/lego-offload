"""Door: the one tool the cloud agent gets, `offload`, served as MCP over HTTP.

Fixed order for every call: label the inputs -> pick a node trusted with all of them ->
choose the reply row from labels and the ledger (charging before anything is read) ->
move the inputs -> run the job on that node -> deliver (full / shaped at the deadline /
receipt). Every decision is a core.py brick; this file only does I/O.
"""
import fcntl
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core
import transport

ABOUT = ("Hand a task to a trusted local model. `inputs` are files: paths relative to your workspace, "
         "or node:/path refs from the catalog or from earlier results. What comes back depends on what "
         "the task touched. Public inputs only: the full output. Private inputs: nothing but a receipt, "
         "unless you pass `returns`, a small answer type, {\"bool\": true}, {\"enum\": [..]}, "
         "{\"int\": [lo, hi]} or {\"tuple\": [..]}; then you get one value of that type (or UNKNOWN) "
         "after a fixed delay, paid from a per-scope bit allowance. Full private outputs are saved for the "
         "user and you get their ref, which you can pass to later tasks.")


class Door:
    def __init__(self, policy, state_dir):
        self.p = policy
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.nodes = {n: core.Node(n, frozenset(v["trust"])) for n, v in policy["nodes"].items()}
        self.public = {f"{policy['host']}:{p}" for p in policy.get("public", [])}
        self.scopes = policy["scopes"]
        core.check_policy(self.public, self.scopes)
        if not set(self.scopes.values()) | {core.SEALED} <= self.nodes[policy["host"]].trust:
            raise core.Refused("the host must be trusted with every scope: all bytes pass through it")
        if policy["nodes"][policy["host"]].get("model"):
            raise core.Refused("the host runs the room, so it cannot also run a model: the agent could time "
                               "the model's work through the shared CPU and GPU")
        self.ledger_file = self.state / "ledger.json"
        if not self.ledger_file.exists():
            self.reset()
        self.refs = {}                                          # result ref -> scopes
        self.salt_key = secrets.token_hex(16)                   # see core.salt
        self.log = (self.state / "door.jsonl").open("a")

    # ---------- state ----------
    def reset(self):
        self.ledger_file.write_text(json.dumps(self.p.get("allowance", {})))

    def _note(self, **kw):
        self.log.write(json.dumps({"t": round(time.time(), 3), **kw}) + "\n")
        self.log.flush()

    def _charge_plan(self, item, schema):
        with self.ledger_file.open("r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            ledger = json.load(f)
            row, new = core.release_plan(item, schema, ledger)
            f.seek(0), f.truncate(), json.dump(new, f)
        return row, new

    # ---------- the tool ----------
    def item(self, ref):
        node, _, path = ref.partition(":")
        if not path.startswith("/"):                           # workspace-relative public file
            node, path = self.p["host"], f"{self.p['workspace']}/{ref}"
            ref = f"{node}:{path}"
        if node not in self.nodes:
            raise core.Refused(f"unknown node in {ref}")
        scopes = self.refs[ref] if ref in self.refs else core.label(ref, self.public, self.scopes)
        return core.Item(ref, scopes, node)

    def pick_node(self, items, want):
        need = frozenset().union(*(i.scopes for i in items))
        names = [want] if want else sorted(self.p["nodes"], key=lambda n: -sum(i.node == n for i in items))
        for n in names:
            if self.p["nodes"].get(n, {}).get("model") and core.may_hold(self.nodes[n], need):
                return n
        raise core.Refused(f"no node with a model is trusted with {sorted(need)}")

    def offload(self, task, inputs=(), returns=None, node=None):
        t0 = time.time()
        items = [self.item(r) for r in inputs]
        n = self.pick_node(items, node)
        out_ref = f"{n}:{self.p['nodes'][n]['out']}/{secrets.token_hex(16)}.txt"
        out = core.Item(out_ref, frozenset().union(*(i.scopes for i in items)), n)
        if returns is not None:
            core.bits(returns)                                 # refuse bad shapes before anything else
        row, ledger = self._charge_plan(out, returns)
        self._note(op="offload", node=n, ref=out_ref, inputs=[i.ref for i in items], scopes=sorted(out.scopes),
                   row=row, bits=core.bits(returns) if row == "shaped" else 0)
        moved = [core.move(i, self.nodes[n]) for i in items]
        self.refs[out_ref] = out.scopes
        result = {}
        job = threading.Thread(target=lambda: result.update(self._run(n, items, moved, task, returns, out, row)))
        job.start()
        if row == "receipt":
            return {"row": "receipt", "ref": out_ref}
        if row == "full":
            job.join()
            return {"row": "full", "ref": out_ref, "output": result.get("output"), "error": result.get("error")}
        deadline = self.p["deadline"]
        job.join(max(0.0, t0 + deadline - time.time()))
        time.sleep(max(0.0, t0 + deadline - time.time()))       # the reply time never depends on the job
        took = result.get("took", deadline + 1) if not job.is_alive() else deadline + 1
        value = core.clock(core.shape(transport.answer(result.get("output")), returns), took, deadline)
        return {"row": "shaped", "answer": value, "ref": out_ref, "allowance_left": ledger}

    def _run(self, n, items, moved, task, returns, out, row):
        node = self.p["nodes"][n]
        try:
            files = [i.ref.partition(":")[2] if i.node == n else
                     transport.copy(self.p["nodes"][i.node], i.ref.partition(":")[2], node, node["stage"])
                     for i in items]
            core.run(moved, self.nodes[n], out.ref)
            r = transport.run_job(node, files, task, returns, out.ref.partition(":")[2], keep=row == "receipt",
                                  timeout=self.p["deadline"] if row == "shaped" else self.p.get("job_timeout", 600),
                                  salt=core.salt(self.salt_key, out.scopes))
            self._note(op="done", ref=out.ref, took=round(r["took"], 2))
            return r
        except Exception as e:                                 # never echo private detail to the cloud
            self._note(op="error", ref=out.ref, error=str(e)[:300])
            return {"error": "job failed"}

    # ---------- MCP over HTTP ----------
    def tools(self):
        cat = "\nCatalog of files you can name in inputs (you cannot read them):\n" + "\n".join(
            f"  {r}  [{', '.join(sorted(self.item(r).scopes)) or 'public'}]" for r in self.p.get("catalog", []))
        return [{"name": "offload", "description": ABOUT + cat, "inputSchema": {
            "type": "object", "required": ["task"], "properties": {
                "task": {"type": "string"}, "inputs": {"type": "array", "items": {"type": "string"}},
                "returns": {"type": "object"}, "node": {"type": "string"}}}}]

    def rpc(self, msg):
        m, mid = msg.get("method"), msg.get("id")
        if m == "initialize":
            res = {"protocolVersion": msg["params"].get("protocolVersion", "2025-03-26"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "lego", "version": "1"}}
        elif m == "tools/list":
            res = {"tools": self.tools()}
        elif m == "tools/call":
            a = msg["params"].get("arguments", {})
            try:
                out = self.offload(a.get("task", ""), a.get("inputs") or [], a.get("returns"), a.get("node"))
            except core.Refused as e:
                self._note(op="refused", reason=str(e))
                out = {"refused": str(e)}
            res = {"content": [{"type": "text", "text": json.dumps(out)}]}
        elif mid is None:
            return None                                        # notification
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {m}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(door, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            reply = door.rpc(msg)
            body = json.dumps(reply).encode() if reply else b""
            self.send_response(200 if reply else 202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
