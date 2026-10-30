"""Door: the agent's two tools, `read` and `offload`, served as MCP over HTTP.

One exchange rule, whichever way data goes (`give`): an item crosses to a model raw if that model
sees its scopes, scrubbed if it sees them scrubbed, and not at all otherwise. The agent's own model
is the caller. It reads through `give`. Its task text and its workspace carry every scope it has
been given, so they cross by the same rule. A job whose inputs the caller could see comes back in
full. A job on more than that comes back as a shaped answer at the deadline, or a receipt.

Fixed order for every offload: label the inputs -> pick a node that may be given all of them ->
queue on that node (one job at a time; a job hidden from the caller also carries the scopes of a
job it may be timed against; a job the caller may see waits until the node is surely idle) ->
choose the reply row from labels and the ledger (charging before anything is read) -> move the
inputs -> run -> reply. Every decision is a core.py brick on the scopes hidden from the caller;
verify/Lego.lean proves this composition for any policy.
"""
import fcntl
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core
import scrub
import transport

LEVELS = {"everything": lambda s: {s, s + "~"}, "scrubbed": lambda s: {s + "~"}, "nothing": lambda s: set()}


def expand(p, state):
    """The simple policy -> the full one the door runs on. Simple form:
    {"private": {scope: dir}, "public": [files], "allowance": bits, "deadline": s,
     "models": {name: {"sees": "everything" | "scrubbed" | "nothing" | {scope: level},
                       "url": ..., "model": ..., "ssh": host, "dir": dir on that host} | {"api": "anthropic", ...}}}"""
    if "models" not in p:
        return p
    host, state = p.get("host", "here"), str(Path(state).resolve())

    def trust(sees):
        sees = dict.fromkeys(p["private"], sees) if isinstance(sees, str) else sees
        if not set(sees) <= set(p["private"]) or not set(sees.values()) <= set(LEVELS):
            raise core.Refused(f"sees must be one of {', '.join(LEVELS)}, or {{scope: level}} for listed scopes")
        return sorted(set().union(*(LEVELS[lvl](s) for s, lvl in sees.items())))

    nodes = {host: {"trust": sorted(set().union(*(LEVELS["everything"](s) for s in p["private"])) | {core.SEALED}),
                    "out": f"{state}/out", "stage": f"{state}/stage"}}
    for name, m in p["models"].items():
        d = m.get("dir", f"{state}/{name}" if not m.get("ssh") else "/tmp/lego")
        nodes[name] = {"trust": trust(m.get("sees", "nothing")), "out": f"{d}/out", "stage": f"{d}/stage",
                       "model": {**{k: m[k] for k in ("api", "url", "model") if k in m}, **({"base_url": m["url"]} if "url" in m else {})}, **({"ssh": m["ssh"]} if m.get("ssh") else {})}
    full = {k: p[k] for k in ("workspace", "deadline", "job_timeout", "catalog") if k in p}
    return {"deadline": 30, "job_timeout": 300, **full, "host": host,
            "public": [os.path.realpath(os.path.expanduser(f)) for f in p.get("public", [])],
            "scopes": {f"{host}:{os.path.realpath(os.path.expanduser(d))}": s for s, d in p["private"].items()},
            "allowance": {s: p.get("allowance", 16) for s in p["private"]}, "nodes": nodes}


class Door:
    def __init__(self, policy, state_dir, caller=None):
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
        self.caller = caller                                    # the agent's own model; None: trusted with nothing
        self.seen = self.nodes[caller].trust if caller else frozenset()
        self.context = set()                                    # scopes given to the caller so far
        self.vault = scrub.Vault(self.state / "vault.json")
        self.ledger_file = self.state / "ledger.json"
        if not self.ledger_file.exists():
            self.reset()
        self.refs = {}                                          # result ref -> scopes
        names = list(policy["nodes"])                           # the queue on each node (core.queue)
        self.slots = {n: (0.0, frozenset()) for n in names}
        self.locks = {n: threading.Lock() for n in names}
        self.turns = {n: threading.Condition() for n in names}
        self.tickets, self.serving = dict.fromkeys(names, 0), dict.fromkeys(names, 0)
        self.jobs = []                                          # background jobs, joined when a session ends
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

    def hidden(self, scopes):
        return frozenset(scopes) - self.seen

    def prepare(self, find=None):
        """Before anything crosses: learn the identifiers of every private file some model sees scrubbed."""
        scrubbed = {s[:-1] for n in self.nodes.values() for s in n.trust if s.endswith("~") and s[:-1] not in n.trust}
        for d, s in self.scopes.items():
            node, _, root = d.partition(":")
            if s not in scrubbed:
                continue
            for path in transport.files(self.p["nodes"][node], root):
                name = f"{node}:{path}"
                if not self.vault.known(name):
                    self.vault.learn(name, transport.read(self.p["nodes"][node], path).decode(errors="replace"), find)

    # ---------- the exchange ----------
    def item(self, ref):
        ref = self.vault.unscrub(ref)                          # a scrubbed name means the real file
        ref = f"{self.p['host']}:{ref}" if ref.startswith("/") else ref      # an absolute path is on the host
        node, _, path = ref.partition(":")
        ws = os.path.realpath(self.p.get("workspace") or "/nonexistent")
        if not path.startswith("/"):                           # relative: a file in the agent's workspace
            node, path = self.p["host"], f"{ws}/{ref}"
        if node not in self.nodes:
            raise core.Refused(f"unknown node in {ref}")
        if node == self.p["host"]:                             # the agent can plant links in its workspace:
            real = os.path.realpath(path)                      # label where the bytes really are
            if real.startswith(ws + "/") and os.path.exists(real) and os.stat(real).st_nlink > 1:
                raise core.Refused(f"{ref} is hardlinked")
            path = real
        ref = f"{node}:{path}"
        scopes = self.refs[ref] if ref in self.refs else core.label(ref, self.public, self.scopes)
        if scopes == {core.SEALED} and path.startswith(ws + "/"):
            scopes = frozenset(self.context)                   # the agent's own files hold what it was given
        return core.Item(ref, scopes, node)

    def give(self, item, n):
        """The exchange rule: (item as node n gets it, scrubbed?) or None."""
        if core.may_hold(self.nodes[n], item.scopes):
            return item, False
        s = core.scrubbed(item.scopes)
        if core.may_hold(self.nodes[n], s):
            return core.Item(item.ref, s, item.node), True
        return None

    def bytes_of(self, item, scrubbed):
        node, _, path = item.ref.partition(":")
        data = transport.read(self.p["nodes"][node], path)
        return self.vault.scrub(data.decode(errors="replace")).encode() if scrubbed else data

    def read(self, ref):
        it = self.item(ref)
        got = self.give(it, self.caller) if self.caller else (it, False) if not it.scopes else None
        if not got:
            raise core.Refused(f"you may not see {sorted(it.scopes)}; ask about it with offload and `returns`")
        self.context |= got[0].scopes
        self._note(op="read", ref=it.ref, scopes=sorted(got[0].scopes), scrubbed=got[1])
        return {"row": "scrubbed" if got[1] else "full", "ref": it.ref, "text": self.bytes_of(*got).decode(errors="replace")}

    def pick_node(self, items, want):
        names = [want] if want else sorted((n for n in self.p["nodes"] if n != self.caller),
                                           key=lambda n: -sum(i.node == n for i in items))
        for n in names:
            if self.p["nodes"].get(n, {}).get("model") and all(self.give(i, n) for i in items):
                return n
        need = frozenset().union(*(i.scopes for i in items))
        raise core.Refused(f"no model may be given {sorted(need)}")

    def decide(self, n, items, returns, t):
        """Everything about a call, from labels, the ledger and node n's queue at time t, before any
        byte is read: the output item, its row, the ledger after, the job's time limit and its start.
        The bricks see only the scopes hidden from the caller."""
        need = frozenset().union(*(i.scopes for i in items))
        extra, start = core.queue(self.slots[n], t, self.hidden(need))
        out = core.Item(f"{n}:{self.p['nodes'][n]['out']}/{secrets.token_hex(16)}.txt", need | extra, n)
        row, ledger = self._charge_plan(core.Item(out.ref, self.hidden(out.scopes), n), returns)
        timeout = self.p["deadline"] if row == "shaped" else self.p.get("job_timeout", 600)
        if self.hidden(need):
            self.slots[n] = core.book(self.slots[n], t, self.hidden(out.scopes), timeout)
        return out, row, ledger, timeout, start

    def offload(self, task, inputs=(), returns=None, node=None):
        items = [self.item(r) for r in inputs]
        text = core.Item("task", frozenset(self.context), self.p["host"])
        if returns is not None:
            core.bits(returns)                                 # refuse bad shapes before anything else
        # a job on what the caller may see comes back in full; otherwise on the raw inputs, shaped
        views = [self.give(i, self.caller) if self.caller else (i, False) if not i.scopes else None
                 for i in items + [text]]
        srcs = [v for v in views] if returns is None and all(views) else [(i, False) for i in items + [text]]
        n = self.pick_node([i for i, _ in srcs], node)
        given = []
        for i, s in srcs:
            g, s2 = self.give(i, n)
            given.append((g, s or s2))
        with self.locks[n]:                                    # one decision at a time per node
            t0 = time.time()
            out, row, ledger, timeout, start = self.decide(n, [g for g, _ in given], returns, t0)
            self._note(op="offload", node=n, ref=out.ref, inputs=[i.ref for i in items], scopes=sorted(out.scopes),
                       scrubbed=[s for _, s in given], row=row, bits=core.bits(returns) if row == "shaped" else 0)
            moved = [core.move(g, self.nodes[n]) for g, _ in given[:-1]]
            self.refs[out.ref] = out.scopes
            turn, self.tickets[n] = self.tickets[n], self.tickets[n] + 1
            task = self.vault.scrub(task) if given[-1][1] else task
            if row == "full":                                  # the caller may see it: the node is surely idle
                time.sleep(max(0.0, start - time.time()))
                r = self._run(n, given[:-1], moved, task, returns, out, row, timeout, turn)
                self.slots[n] = (time.time(), frozenset())
                output = r.get("output")
                if output and self.caller and any(s for _, s in given) and all(
                        core.may_hold(self.nodes[self.caller], {s[:-1]}) for s in out.scopes if s.endswith("~")):
                    output = self.vault.unscrub(output)        # the caller sees the real values
                self.context |= out.scopes
                return {"row": "full", "ref": out.ref, "output": output, "error": r.get("error")}
        result = {}
        job = threading.Thread(target=lambda: result.update(self._run(n, given[:-1], moved, task, returns, out, row,
                                                                      timeout, turn)))
        job.start()
        self.jobs.append(job)
        if row == "receipt":
            return {"row": "receipt", "ref": out.ref}
        deadline = self.p["deadline"]
        job.join(max(0.0, t0 + deadline - time.time()))
        time.sleep(max(0.0, t0 + deadline - time.time()))       # the reply time never depends on the job
        value = core.clock(core.shape(transport.answer(result.get("output")), returns),
                           0 if not job.is_alive() else deadline + 1, deadline)
        return {"row": "shaped", "answer": value, "ref": out.ref, "allowance_left": ledger}

    def _run(self, n, given, moved, task, returns, out, row, timeout, turn):
        node = self.p["nodes"][n]
        with self.turns[n]:                                    # first in, first out: one job at a time
            self.turns[n].wait_for(lambda: self.serving[n] == turn)
        try:
            files = [transport.put(node, node["stage"], self.vault.scrub(i.ref.rsplit("/", 1)[-1]), self.bytes_of(i, True)) if s else
                     i.ref.partition(":")[2] if i.node == n else
                     transport.copy(self.p["nodes"][i.node], i.ref.partition(":")[2], node, node["stage"])
                     for i, s in given]
            core.run(moved, self.nodes[n], out.ref)
            r = transport.run_job(node, files, task, returns, out.ref.partition(":")[2], keep=row == "receipt",
                                  timeout=timeout, salt=core.salt(self.salt_key, out.scopes))
            self._note(op="done", ref=out.ref, took=round(r["took"], 2))
            return r
        except Exception as e:                                 # never echo private detail to the cloud
            self._note(op="error", ref=out.ref, error=str(e)[:300])
            return {"error": "job failed"}
        finally:
            with self.turns[n]:
                self.serving[n] += 1
                self.turns[n].notify_all()

    # ---------- MCP over HTTP ----------
    def level(self, scope):
        t = self.seen if self.caller else frozenset()
        return "everything" if scope in t else "scrubbed" if scope + "~" in t else "nothing"

    def about(self):
        """What the caller sees, scope by scope, and how to use the tools: the agent knows nothing else of lego."""
        others = [n for n, v in self.p["nodes"].items() if v.get("model") and n != self.caller]
        lines = []
        for d, s in self.scopes.items():
            lvl = self.level(s)
            lines.append(f"  {d.partition(':')[2]}  [{s}]  you see: {lvl}" + {
                "everything": "", "scrubbed": " (same data, with names, emails, phones, addresses, account "
                "numbers and keys replaced by stable placeholders such as [PERSON_3])",
                "nothing": " (ask small typed questions with offload `returns`, or get a receipt)"}[lvl])
            if lvl != "nothing":
                node, _, root = d.partition(":")
                for f in transport.files(self.p["nodes"][node], root)[:40]:
                    lines.append("    " + (f if lvl == "everything" else self.vault.scrub(f)))
        lines += [f"  {r}" for r in self.p.get("catalog", [])]
        return ("lego guards the user's private files. You see only what the policy lets you see; everything "
                "crossing between models passes through the same rule, so you never get more than your level, "
                "and anything you send is cut down to the receiver's level.\n"
                f"Private data:\n" + ("\n".join(lines) or "  (none)") +
                "\nUnlisted files outside your workspace are sealed and refused.\n"
                f"Models you can offload to: {', '.join(others) or 'none'}.\n"
                "Tools: `read` gets a file at your level (raw or scrubbed). `offload` hands a task to another "
                "model: it sees only your task text and the files you list in `inputs`, nothing else, so write "
                "complete instructions and name every file. If you could see all the inputs, you get the full "
                "result. On inputs you may not see, pass `returns` (one small typed answer: {\"bool\": true}, "
                "{\"enum\": [..]}, {\"int\": [lo, hi]} or {\"tuple\": [..]}), which arrives after a fixed delay "
                "and is paid from a per-scope bit allowance; without `returns` you get a receipt (the result is "
                "saved for the user, and its ref can be an input to a later task).")

    def tools(self):
        return [{"name": "read", "description": "Read a file at your level: raw if you see its scope, scrubbed if you "
                 "see it scrubbed, refused otherwise.\n\n" + self.about(),
                 "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
                {"name": "offload", "description": "Hand a task to another model, which works on the inputs at its "
                 "own level and replies at yours.\n\n" + self.about(), "inputSchema": {
                     "type": "object", "required": ["task"], "properties": {
                         "task": {"type": "string", "description": "complete instructions; the model sees nothing else"},
                         "inputs": {"type": "array", "items": {"type": "string"},
                                    "description": "absolute paths, node:/path refs, or workspace-relative paths"},
                         "returns": {"type": "object", "description": "a typed answer, for inputs you may not see"},
                         "node": {"type": "string", "description": "which model; default: the first that may be given the inputs"}}}}]

    def rpc(self, msg):
        m, mid = msg.get("method"), msg.get("id")
        if m == "initialize":
            res = {"protocolVersion": msg["params"].get("protocolVersion", "2025-03-26"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "lego", "version": "2"},
                   "instructions": self.about()}
        elif m == "tools/list":
            res = {"tools": self.tools()}
        elif m == "tools/call":
            a, name = msg["params"].get("arguments", {}), msg["params"].get("name")
            try:
                if not isinstance(a, dict) or not isinstance(a.get("inputs") or [], list):
                    raise core.Refused("arguments must be {task, inputs: [..], returns, node}")
                if name == "read":
                    out = self.read(str(a.get("path", "")))
                else:
                    out = self.offload(str(a.get("task", "")), a.get("inputs") or [], a.get("returns"), a.get("node"))
            except core.Refused as e:
                self._note(op="refused", reason=str(e))
                out = {"refused": str(e)}
            except Exception as e:                             # fail closed: a call that breaks gets nothing
                self._note(op="refused", reason=f"call failed ({type(e).__name__})")
                out = {"refused": f"call failed ({type(e).__name__})"}
            res = {"content": [{"type": "text", "text": out.pop("text") if "text" in out else json.dumps(out)}]}
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
            try:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                reply = door.rpc(msg) if isinstance(msg, dict) else None
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad request"}}
            body = json.dumps(reply).encode() if reply else b""
            self.send_response(200 if reply else 202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
