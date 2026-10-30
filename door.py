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

    nodes = {host: {"trust": sorted({*trust("everything"), core.SEALED}), "out": f"{state}/out", "stage": f"{state}/stage"}}
    for name, m in p["models"].items():
        d = m.get("dir", f"{state}/{name}" if not m.get("ssh") else "/tmp/lego")
        model = {k: m[k] for k in ("api", "url", "model") if k in m}
        nodes[name] = {"trust": trust(m.get("sees", "nothing")), "out": f"{d}/out", "stage": f"{d}/stage",
                       "model": {**model, **({"base_url": m["url"]} if "url" in m else {})}, **({"ssh": m["ssh"]} if m.get("ssh") else {})}
    full = {k: p[k] for k in ("workspace", "deadline", "job_timeout", "catalog") if k in p}
    return {"deadline": 30, "job_timeout": 300, **full, "host": host,
            "public": [os.path.realpath(os.path.expanduser(f)) for f in p.get("public", [])],
            "scopes": {f"{host}:{os.path.realpath(os.path.expanduser(d))}": s for s, d in p["private"].items()},
            "allowance": {s: p.get("allowance", 16) for s in p["private"]}, "nodes": nodes}


def inside(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/")


def runnable(v):
    """A node whose model the door can call: a local endpoint, or Claude through the user's login."""
    m = v.get("model")
    return bool(m) and not (isinstance(m, dict) and m.get("api") not in (None, "anthropic"))


class Door:
    def __init__(self, policy, state_dir, caller=None):
        self.p = policy
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.host = policy["host"]
        self.nodes = {n: core.Node(n, frozenset(v["trust"])) for n, v in policy["nodes"].items()}
        self.public = {f"{self.host}:{p}" for p in policy.get("public", [])}
        self.scopes = policy["scopes"]
        core.check_policy(self.public, self.scopes)
        if not set(self.scopes.values()) | {core.SEALED} <= self.nodes[self.host].trust:
            raise core.Refused("the host must be trusted with every scope: all bytes pass through it")
        if policy["nodes"][self.host].get("model"):
            raise core.Refused("the host runs the room, so it cannot also run a model: the agent could time "
                               "the model's work through the shared CPU and GPU")
        self.ws = os.path.realpath(policy.get("workspace") or "/nonexistent")
        mine = [str(self.state.resolve())] + [d.partition(":")[2] for d in self.scopes if d.startswith(self.host + ":")] + [
            v[k] for v in policy["nodes"].values() if not v.get("ssh") for k in ("out", "stage") if k in v]
        if any(inside(self.ws, os.path.realpath(d)) or inside(os.path.realpath(d), self.ws) for d in mine):
            raise core.Refused("the workspace overlaps a private directory or lego's own state: the room could read it")
        self.caller = caller                                    # the agent's own model
        self.me = self.nodes[caller] if caller else core.Node("", frozenset())
        self.seen = self.me.trust
        self.taint_file = self.state / "taint.json"             # what the workspace holds, across sessions
        self.context = set(self._taints().get(self.ws, []))     # scopes given to the caller (its workspace too)
        self.vault = scrub.Vault(self.state / "vault.json")
        self.ledger_file = self.state / "ledger.json"
        if not self.ledger_file.exists():
            self.reset()
        self.refs, self.alias, self.inodes = {}, {}, {}         # result ref -> scopes; shown name -> ref; ref -> inode
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

    def _taints(self):
        return json.loads(self.taint_file.read_text()) if self.taint_file.exists() else {}

    def taint(self, scopes):
        """The caller was given these scopes: so were its workspace files, for this and every later session."""
        self.context |= set(scopes)
        with open(self.taint_file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            t = json.loads(f.read() or "{}")
            t[self.ws] = sorted(set(t.get(self.ws, [])) | self.context)
            f.seek(0), f.truncate(), f.write(json.dumps(t))

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

    def prepare(self, find=None, strict=False):
        """Before anything crosses: learn the identifiers of every private file some model sees scrubbed.
        find(scope) gives a function that lists identifiers with a local model trusted with that scope."""
        scrubbed = {s[:-1] for n in self.nodes.values() for s in n.trust if s.endswith("~") and s[:-1] not in n.trust}
        for d in [d for d, s in self.scopes.items() if s in scrubbed]:
            node, _, root = d.partition(":")
            f = find(self.scopes[d]) if find else None
            if strict and not f:
                raise core.Refused(f"{self.scopes[d]} is seen scrubbed, but no local model sees it raw to find its names")
            for path in transport.files(self.p["nodes"][node], root):
                data = transport.read(self.p["nodes"][node], path)
                if not self.vault.known(f"{node}:{path}", data):
                    self.vault.learn(f"{node}:{path}", data, [self.scopes[d]], f)

    # ---------- the exchange ----------
    def item(self, ref):
        """The caller's name for some data -> an item with its scopes. Refusals echo only `asked`."""
        asked, ref = ref, self.alias.get(ref, ref)             # a name shown scrubbed means its real file
        mine = [r for r in self.refs if r.partition(":")[2] == ref]
        ref = mine[0] if len(mine) == 1 else ref               # a result ref given without its node
        ref = f"{self.host}:{ref}" if ref.startswith("/") else ref
        node, _, path = ref.partition(":")
        if not path.startswith("/"):                           # relative: a file in the agent's workspace
            node, path = self.host, f"{self.ws}/{ref}"
        if node not in self.nodes:
            raise core.Refused(f"unknown node in {asked}")
        if node == self.host:                                  # the agent can plant links in its workspace:
            path = os.path.realpath(path)                      # label where the bytes really are, and pin them
            if os.path.exists(path):
                st = os.stat(path)
                if inside(path, self.ws) and st.st_nlink > 1 and not os.path.isdir(path):
                    raise core.Refused(f"{asked} is hardlinked")
                self.inodes[f"{node}:{path}"] = (st.st_dev, st.st_ino)
        ref = f"{node}:{path}"
        scopes = self.refs[ref] if ref in self.refs else core.label(ref, self.public, self.scopes)
        if scopes == {core.SEALED} and inside(path, self.ws) and node == self.host:
            scopes = frozenset(self.context)                   # the agent's own files hold what it was given
        return core.Item(ref, scopes, node)

    def give(self, item, node):
        """The exchange rule: (item as `node` gets it, scrubbed?) or None."""
        if core.may_hold(node, item.scopes):
            return item, False
        s = core.scrubbed(item.scopes)
        return ((core.Item(item.ref, s, item.node), True) if core.may_hold(node, s) else None)

    def bytes_of(self, item, scrubbed, timeout=60):
        """An item's bytes, scrubbed if asked. A host file is opened without following links and must
        be the very file that was labelled."""
        node, _, path = item.ref.partition(":")
        if node == self.host:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                st = os.fstat(fd)
                if self.inodes.get(item.ref) != (st.st_dev, st.st_ino):
                    raise core.Refused("the file changed after it was labelled")
                data = b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
            finally:
                os.close(fd)
        else:
            data = transport.read(self.p["nodes"][node], path, timeout)
        tags = {s.rstrip("~") for s in item.scopes}
        return self.vault.scrub(data.decode(errors="replace"), tags).encode() if scrubbed else data

    def level(self, scope, node=None):
        t = (node or self.me).trust
        return "everything" if scope in t else "scrubbed" if scope + "~" in t else "nothing"

    def catalog(self):
        """The private directories, each at the caller's level: real names, scrubbed names, or a count."""
        lines = []
        for d, s in self.scopes.items():
            node, _, root = d.partition(":")
            lvl, found = self.level(s), transport.files(self.p["nodes"][node], root)
            shown = root if node == self.host else d
            lines.append(f"  {shown}  [{s}]  you see: {lvl}" + {
                "everything": "", "scrubbed": " (same data; names, emails, phones, addresses, account numbers "
                "and keys replaced by stable placeholders such as [PERSON_3])",
                "nothing": f" ({len(found)} files, names hidden: pass the directory to offload with `returns`)"}[lvl])
            for f in found[:60] if lvl != "nothing" else []:
                name = f"{shown}{f[len(root):]}" if lvl == "everything" else \
                    f"{shown}{self.vault.scrub_name(f[len(root):], [s])}"
                self.alias[name] = f"{node}:{f}"
                lines.append("    " + name)
        lines += [f"  {r}" for r in self.p.get("catalog", [])]
        return "\n".join(lines) or "  (none)"

    def listing(self):
        files = []
        for root, dirs, names in os.walk(self.ws):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            files += [os.path.relpath(os.path.join(root, n), self.ws) for n in sorted(names)]
        return (f"Your workspace {self.ws} (relative paths mean files here):\n" +
                "\n".join("  " + f for f in files[:200]) + "\nPrivate directories:\n" + self.catalog())

    def read(self, ref):
        it = self.item(ref.strip() or ".")
        path = it.ref.partition(":")[2]
        if it.node == self.host and os.path.isdir(path) or any(inside(d.partition(":")[2], path) for d in self.scopes):
            return {"row": "listing", "text": self.listing()}  # a directory: what is in it, at your level
        if core.SEALED in it.scopes:
            raise core.Refused(f"{ref} is sealed. You can read files listed under the private directories, files in "
                               f"your workspace {self.ws}, and offload refs as returned. read \".\" lists them.")
        got = self.give(it, self.me)
        if not got:
            raise core.Refused(f"you may not see {sorted(it.scopes)}; ask about it with offload and `returns`")
        if it.node == self.host and not os.path.isfile(path):
            raise core.Refused(f"no file {ref}. read \".\" lists what you can name.")
        text = self.bytes_of(*got).decode(errors="replace")
        self.taint(got[0].scopes)
        self._note(op="read", ref=it.ref, scopes=sorted(got[0].scopes), scrubbed=got[1])
        return {"row": "scrubbed" if got[1] else "full", "text": text}

    def pick_node(self, items, want):
        models = [n for n, v in self.p["nodes"].items() if runnable(v)]
        if want and want not in models:
            raise core.Refused(f"no model named {want}; models: {', '.join(models)}")
        names = [want] if want else sorted((n for n in models if n != self.caller),
                                           key=lambda n: -sum(i.node == n for i in items))
        for n in names:
            if all(self.give(i, self.nodes[n]) for i in items):
                return n
        need = frozenset().union(*(i.scopes for i in items))
        held = " (your context holds what you have read, and it goes with every task)" if self.context else ""
        raise core.Refused(f"no model may be given {sorted(need)}{held}")

    def decide(self, n, items, returns, t):
        """Everything about a call, from labels, the ledger and node n's queue at time t, before any
        byte is read: the output item, its row, the ledger after, when the job may start and when it
        must end. The bricks see only the scopes hidden from the caller."""
        need = frozenset().union(*(i.scopes for i in items))
        extra, start = core.queue(self.slots[n], t, self.hidden(need))
        out = core.Item(f"{n}:{self.p['nodes'][n]['out']}/{secrets.token_hex(16)}.txt", need | extra, n)
        row, ledger = self._charge_plan(core.Item(out.ref, self.hidden(out.scopes), n), returns)
        timeout = self.p["deadline"] if row == "shaped" else self.p.get("job_timeout", 600)
        if self.hidden(need):
            self.slots[n] = core.book(self.slots[n], t, self.hidden(out.scopes), timeout)
        return out, row, ledger, start, self.slots[n][0] if self.hidden(need) else start + timeout

    def items(self, refs):
        """Inputs as items. A directory stands for every file in it, so a model that may not see the
        file names can still name the data."""
        out = []
        for r in refs:
            it = self.item(r)
            node, _, path = it.ref.partition(":")
            found = [] if it.ref in self.refs or core.SEALED in it.scopes else transport.files(self.p["nodes"][node], path)
            out += [self.item(f"{node}:{f}") for f in found] if found and found != [path] else [it]
        return out

    def offload(self, task, inputs=(), returns=None, node=None):
        ins = self.items(inputs) + [core.Item("task", frozenset(self.context), self.host)]   # the task text is last
        if returns is not None:
            core.bits(returns)                                 # refuse bad shapes before anything else
        # a job on what the caller may see comes back in full; otherwise on the raw inputs, shaped
        views = [self.give(i, self.me) for i in ins]
        srcs = views if returns is None and all(views) else [(i, False) for i in ins]
        n = self.pick_node([i for i, _ in srcs], node)
        given = [(g, s or s2) for (i, s), (g, s2) in zip(srcs, (self.give(i, self.nodes[n]) for i, _ in srcs))]
        everything = set(self.scopes.values()) <= self.nodes[n].trust
        scrub_task = given[-1][1] or not everything            # names it saw in this tool's text, too
        task = self.vault.scrub(task, self.scopes.values()) if scrub_task else task
        with self.locks[n]:                                    # one decision at a time per node
            t0 = time.time()
            out, row, ledger, start, end = self.decide(n, [g for g, _ in given], returns, t0)
            self._note(op="offload", node=n, ref=out.ref, inputs=[i.ref for i in ins[:-1]], scopes=sorted(out.scopes),
                       scrubbed=[s for _, s in given], row=row, bits=core.bits(returns) if row == "shaped" else 0)
            self.refs[out.ref] = out.scopes
            turn, self.tickets[n] = self.tickets[n], self.tickets[n] + 1
            run = lambda: self._run(n, given[:-1], task, returns, out, row, end, turn, everything)  # noqa: E731
            if row == "full":                                  # the caller may see it: the node is surely idle
                time.sleep(max(0.0, start - time.time()))
                r = run()
                self.slots[n] = (time.time(), frozenset())
                output = r.get("output")
                if output and (scrub_task or any(s for _, s in given)):
                    output = self.vault.unscrub(output, {s for s in self.seen if not s.endswith("~")})
                self.taint(out.scopes)
                return {"row": "full", "ref": out.ref, "output": output, "error": r.get("error")}
        result = {}
        job = threading.Thread(target=lambda: result.update(run()))
        job.start()
        self.jobs.append(job)
        if row == "receipt":
            return {"row": "receipt", "ref": out.ref}
        deadline = self.p["deadline"]
        job.join(max(0.0, t0 + deadline - time.time()))
        time.sleep(max(0.0, t0 + deadline - time.time()))       # the reply time never depends on the job
        value = core.clock(core.shape(transport.answer(result.get("output")), returns),
                           0 if not job.is_alive() else deadline + 1, deadline)
        self._note(op="answer", ref=out.ref, answer=value)
        return {"row": "shaped", "answer": value, "ref": out.ref, "allowance_left": ledger}

    def _run(self, n, given, task, returns, out, row, end, turn, everything):
        """One job on node n. Staging and the model call both stop by `end`, the bound its slot was
        booked with, so no later job's timing depends on how long this one would have run."""
        node = self.p["nodes"][n]
        left = lambda: max(1.0, end - time.time())             # noqa: E731
        with self.turns[n]:                                    # first in, first out: one job at a time
            self.turns[n].wait_for(lambda: self.serving[n] == turn)
        try:
            files = []
            for i, s in given:
                name = i.ref.rsplit("/", 1)[-1]
                name = name if everything and not s else self.vault.scrub_name(name, {x.rstrip("~") for x in i.scopes})
                files.append(i.ref.partition(":")[2] if i.node == n and not s else
                             transport.put(node, node["stage"], name, self.bytes_of(i, s, left()), left()))
            core.run([core.move(g, self.nodes[n]) for g, _ in given], self.nodes[n], out.ref)
            r = transport.run_job(node, files, task, returns, out.ref.partition(":")[2], keep=row == "receipt",
                                  timeout=left(), salt=core.salt(self.salt_key, out.scopes))
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
    def about(self):
        """Everything the agent knows of lego: its level per directory, the other models' levels, the rules."""
        others = [n for n, v in self.p["nodes"].items() if runnable(v) and n != self.caller]
        kinds = sorted(set(self.scopes.values()))
        return ("lego guards the user's private files. Your own shell and file tools cannot open them; the `read` "
                "and `offload` tools can, at the level shown.\nPrivate directories:\n" + self.catalog() +
                f"\nYour workspace: {self.ws}. Relative paths mean files there. Anything else is sealed.\n"
                "Other models: " + ("; ".join(f"{n} (sees " + ", ".join(f"{s}: {self.level(s, self.nodes[n])}"
                                                                       for s in kinds) + ")" for n in others) or "none") +
                ".\nWhat you read stays in your context: every task you offload carries it, and a model that may not "
                "see it refuses the task for the rest of this session.\n"
                "`offload` gives another model your task text and the `inputs` files, nothing else: write complete "
                "instructions and list every file (a directory stands for all its files). If you could see all the "
                "inputs, you get the full result. On inputs you may not see, pass `returns`, one small typed answer "
                "paid from a per-scope bit allowance and sent after a fixed delay; without it you get a receipt "
                "(the result is saved for the user; its ref can be an input later).")

    def tools(self):
        about = self.about()
        return [{"name": "read", "description": "Read a file at your level: raw, scrubbed, or refused. `read \".\"` "
                 "lists your workspace and the private files you can name.\n\n" + about, "inputSchema": {
                     "type": "object", "required": ["path"], "properties": {"path": {
                         "type": "string", "description": "a listed path, a workspace-relative path, or an offload ref"}}}},
                {"name": "offload", "description": "Hand a task to another model (see the read tool for the rules).",
                 "inputSchema": {"type": "object", "required": ["task"], "properties": {
                     "task": {"type": "string", "description": "complete instructions; the model sees nothing else"},
                     "inputs": {"type": "array", "items": {"type": "string"},
                                "description": "listed paths or directories, workspace-relative paths, or offload refs"},
                     "returns": {"type": "object", "description": 'a typed answer for inputs you may not see: {"bool": '
                                 'true}, {"enum": ["a", "b"]}, {"int": [0, 10]} or {"tuple": [{"bool": true}, {"int": [0, 3]}]}'},
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
                    out = self.read(str(a.get("path") or a.get("file_path") or ""))
                else:
                    out = self.offload(str(a.get("task", "")), a.get("inputs") or [], a.get("returns"), a.get("node"))
            except Exception as e:                             # fail closed: a call that breaks gets nothing
                why = str(e) if isinstance(e, core.Refused) else f"call failed ({type(e).__name__})"
                self._note(op="refused", reason=why)
                out = {"refused": why}
            res = {"content": [{"type": "text", "text": out.pop("text") if "text" in out else json.dumps(out)}],
                   "isError": "refused" in out}
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
