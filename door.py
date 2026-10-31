"""Door: the agent's two tools, `read` and `offload`, served as MCP over HTTP.

One exchange rule, whichever way data goes (`give`): an item crosses to a model raw if that model
sees its scopes, scrubbed if it sees them scrubbed, and not at all otherwise. The agent's own model
is the caller. It reads through `give`. Its task text and its workspace carry every scope it has
been given, so they cross by the same rule. A job whose inputs the caller could see comes back in
full. A job on more than that comes back as a shaped answer at the deadline, or a receipt.

Fixed order for every offload: label the inputs -> pick a node that may be given all of them -> queue on that node
(one job at a time; a job hidden from the caller also carries the scopes of a job it may be timed against; a job the
caller may see waits until the node is surely idle) -> choose the reply row from labels and the ledger (charging
before anything is read) -> move the inputs -> run -> reply. Every decision is a core.py brick on the scopes hidden
from the caller; verify/Lego.lean proves this composition for any policy."""
import collections
import concurrent.futures
import contextlib
import fcntl
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
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
            "public": [canon(f) for f in p.get("public", [])],
            "scopes": {f"{host}:{canon(d)}": s for s, d in p["private"].items()},
            "allowance": {s: p.get("allowance", 16) for s in p["private"]}, "nodes": nodes}


def canon(p):
    """One spelling per file: links resolved, and on macOS the on-disk case without firmlinks, so two
    spellings of a directory (Data and data, /System/Volumes/Data/...) are one directory everywhere."""
    p = os.path.realpath(os.path.expanduser(p))
    if not os.path.exists(p):
        head, tail = os.path.split(p)
        return p if head == p else os.path.join(canon(head), tail)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return p
    try:
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode() if hasattr(fcntl, "F_GETPATH") else p
    finally:
        os.close(fd)


def getpath(fd):
    """An open file's path as the disk spells it (macOS), or None."""
    try:
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode() if hasattr(fcntl, "F_GETPATH") else None
    except OSError:
        return None


def spelled(dirfd, name):
    """The on-disk spelling of `name` in an open folder, not following it if it is a link (None: no open)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    except OSError:
        return None
    try:
        return getpath(fd)
    finally:
        os.close(fd)


def bounded(f, timeout):
    """f() in a thread, waited on until the timeout and no longer (a late f is left to finish unread)."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=f()), daemon=True)
    t.start()
    t.join(timeout)
    if "v" not in box:
        raise TimeoutError("staging reached the job's bound")
    return box["v"]


def literals(schema):
    """The values a `returns` schema spells out: enum members and int bounds, in tuples too."""
    return [*schema.get("enum", []), *schema.get("int", []), *(x for t in schema.get("tuple", []) for x in literals(t))]


def here(node):
    """Does a node keep its files on this machine?"""
    return not node.get("ssh")


def measured(node):
    """Could the room measure a node's job files? Yes for any on this machine: df reads every volume."""
    return here(node)


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
        for s in set(self.scopes.values()):                    # hr~ is hr scrubbed, sealed is no scope
            if not re.fullmatch(r"[A-Za-z0-9_-]+", s) or s == core.SEALED:
                raise core.Refused(f"scope name {s!r}: use letters, digits, - and _ only, and not {core.SEALED!r}")
        disk = lambda d: d.partition(":")[2] if here(policy["nodes"].get(d.partition(":")[0], {})) else d  # noqa: E731
        for d in self.scopes:                                  # a listing of the outer one would show the inner
            if d.endswith("~") or "," in d:                    # taint ids: dirs joined by "," and "~" for scrubbed
                raise core.Refused(f"private directory {d} may not end in ~ or hold a comma")
            if any(inside(disk(d), disk(o)) or inside(disk(o), disk(d)) and o in self.public
                   for o in [*self.public, *self.scopes] if o != d):   # local nodes' folders are on this disk too
                raise core.Refused(f"private directory {d} is inside a public path or another private directory")
        if not set(self.scopes.values()) | {core.SEALED} <= self.nodes[self.host].trust:
            raise core.Refused("the host must be trusted with every scope: all bytes pass through it")
        if policy["nodes"][self.host].get("model"):
            raise core.Refused("the host runs the room, so it cannot also run a model: the agent could time "
                               "the model's work through the shared CPU and GPU")
        self.ws = canon(policy.get("workspace") or "/nonexistent")
        self.taint_file = Path(os.environ.get("LEGO_TAINT", "~/.lego/taint.json")).expanduser()  # per workspace,
        self.taint_file.parent.mkdir(parents=True, exist_ok=True)                            # every policy
        if any(inside(canon(d), self.ws) for d in (self.taint_file.parent, Path("~/.lego").expanduser())):
            raise core.Refused("the workspace holds lego's taints and locks: the room could rewrite them")
        self.roots = [d.partition(":")[2] for d in self.scopes if d.startswith(self.host + ":")]   # private dirs here
        self.stops = self.roots + [d.partition(":")[2] for d in self.scopes if here(policy["nodes"].get(
            d.partition(":")[0], {}))] + [v[k] for v in policy["nodes"].values() if here(v) for k in ("out", "stage") if k in v]
        mine = [str(self.state.resolve())] + self.stops        # every private folder on this disk
        if any(inside(self.ws, canon(d)) or inside(canon(d), self.ws) for d in mine):
            raise core.Refused("the workspace overlaps a private directory or lego's own state: the room could read it")
        self.caller = caller                                    # the agent's own model
        self.me = self.nodes[caller] if caller else core.Node("", frozenset())
        self.seen = self.me.trust
        self.raw = {s for s in self.seen if not s.endswith("~")}  # scopes the caller sees as written
        for n, v in policy["nodes"].items():                   # a hidden job's size must not show in df, which
            if runnable(v) and measured(v) and n != self.host and \
                    set(self.scopes.values()) & set(v["trust"]) - self.seen:   # sees every volume of this machine
                raise core.Refused(f"{n} would keep jobs on data hidden from {caller} on this machine, where "
                                   f"{caller}'s agent could measure them: give {n} ssh to another machine")
        servers = collections.Counter((v.get("ssh"), m["base_url"]) for v in policy["nodes"].values()
                                      if runnable(v) and isinstance(m := v["model"], dict) and m.get("base_url"))
        if any(c > 1 for c in servers.values()):               # the queue is per name: two names on one server
            raise core.Refused("two models in the policy are one model server: give it one name")   # would time each other
        near = {w: t for w, t in self._taints().items() if inside(canon(w.lstrip("@")), self.ws)
                or inside(self.ws, canon(w.lstrip("@")))}      # this workspace, and ones nested in or around it
        self.context = {s for w, t in near.items() if not w.startswith("@") for s in t}   # given to the caller
        self.held = self.context | {s for w, t in near.items() if w.startswith("@") for s in t}  # and what an agent
        if self.raw & set(self.scopes.values()):               # that sees them raw may have typed there
            self._record("@" + self.ws, self.raw & set(self.scopes.values()))
        self.vault = scrub.Vault(self.state / "vault.json")
        self.vault.bind({s: self._id(s) for s in set(self.scopes.values())})
        self.ledger_file = self.state / "ledger.json"
        if not self.ledger_file.exists():
            self.reset()
        self.refs, self.alias, self.inodes = {}, {}, {}         # result ref -> scopes; shown name -> ref; ref -> inode
        self.found = {}                                        # scope dir -> its files, listed before the agent starts
        self.learned = set()                                    # files the catalog found in the vault
        self.deferred = set()                                   # hidden inputs, expanded only inside their job
        self.views, self.names = {}, {}                         # scrubbed views and names, made at the start
        self.snap = {}                                          # files on other machines, as lego found them
        self.staged = {}                                        # (node, from, scope dir) -> its copy there
        self.fresh = {}                                         # the caller's own words -> placeholders, and back
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
        """Taints as this policy's scope names. A scope is recorded as its directories, so another policy
        reusing the name for other data does not match; data no scope here holds is SEALED (no one)."""
        name = {self._id(s): s for s in set(self.scopes.values())}
        t = json.loads(self.taint_file.read_text()) if self.taint_file.exists() else {}
        return {w: [name.get(x.rstrip("~"), core.SEALED) + ("~" if x.endswith("~") else "") for x in v] for w, v in t.items()}

    def _id(self, scope):
        return ",".join(sorted(d for d, s in self.scopes.items() if s == scope.rstrip("~"))) + ("~" if scope.endswith("~") else "")

    def taint(self, scopes):
        """The caller was given these scopes: so were its workspace files, for this and every later session."""
        self.context |= set(scopes)
        self.held |= set(scopes)
        self._record(self.ws, self.context)

    def _record(self, key, scopes):
        with open(self.taint_file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            t = json.loads(f.read() or "{}")
            t[key] = sorted(set(t.get(key, [])) | {self._id(s) for s in scopes})
            f.seek(0), f.truncate(), f.write(json.dumps(t))

    def _note(self, **kw):
        """One record of one size: the log's growth, which df shows, never depends on what it holds."""
        line = json.dumps({"t": round(time.time(), 3), **kw})
        if len(line) >= 4096:                                  # a long one keeps what says what happened
            line = json.dumps({"t": round(time.time(), 3), "long": True,
                               **{k: v for k, v in kw.items() if k in ("op", "node", "ref", "row", "bits")}})
        self.log.write(line.ljust(4095) + "\n")
        self.log.flush()

    def listed(self, node, path):
        """A folder's files. On another machine, as lego found them at the start: nothing crosses now."""
        if self.p["nodes"][node].get("ssh"):
            for d, found in self.found.items():
                if d.partition(":")[0] == node and inside(path, d.partition(":")[2]):
                    return [f for f in found if inside(f, path)]
        return transport.files(self.p["nodes"][node], path)

    def whole(self, i, s, n):
        """Does input i cross to node n as its whole scope? When it goes raw and is not the caller's to see."""
        return i.ref in self.deferred or not s and i.node != n and i.ref not in self.refs and not core.may_hold(
            self.me, i.scopes) and i.ref != "task"

    def hidden(self, scopes):
        return frozenset(scopes) - self.seen

    def prepare(self, find=None, strict=False):
        """Before anything crosses: learn the identifiers of every private file some model may not see raw.
        find(scope) gives a function that lists identifiers with a local model trusted with that scope."""
        scrubbed = {s for n, v in self.p["nodes"].items() if runnable(v) for s in set(self.scopes.values()) - self.nodes[n].trust}
        datas, todo = {}, []
        for d in [d for d, s in self.scopes.items() if s in scrubbed]:
            node, _, root = d.partition(":")
            f = find(self.scopes[d]) if find else None
            if strict and not f:
                raise core.Refused(f"{self.scopes[d]} is hidden from some model, but no local model sees it raw to find its names")
            for path in self.found.setdefault(d, transport.files(self.p["nodes"][node], root)):
                data = datas[f"{node}:{path}"] = transport.read(self.p["nodes"][node], path, 600)
                if not self.vault.known(f"{node}:{path}", data):
                    todo.append((f"{node}:{path}", data, self.scopes[d], f))
                if self.p["nodes"][node].get("ssh"):           # on another machine: its scrubbed views are made
                    self.snap[f"{node}:{path}"] = data         # from this copy, so nothing crosses at request time
        also = [self.rel(ref, scope) for ref, _, scope, _ in todo]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:   # the local model reads every file at once
            ids = list(pool.map(lambda t, rel: t[3](scrub.searched(t[1], rel)[1]) if t[3] else [], todo, also))
        for (ref, data, scope, _), rel, got in zip(todo, also, ids):
            self.vault.learn(ref, data, [scope], lambda text, got=got: got, also=rel)
        for d in self.scopes:                                  # a listing never walks a scope at request time:
            node, _, root = d.partition(":")                   # its time would show the hidden folders
            self.found.setdefault(d, transport.files(self.p["nodes"][node], root))
        for s in scrubbed:                                     # matchers built now, from what was learned: a
            self.vault._pattern(s)                             # first call's time would show how much it holds
        for d in [d for d, s in self.scopes.items() if s in scrubbed]:   # and every view, now: how long scrubbing
            node, _, root = d.partition(":")                   # takes depends on what the tables hold, so a
            for path in self.found[d]:                         # request only looks one up
                self.views[f"{node}:{path}", self.scopes[d]] = self.view(datas[f"{node}:{path}"], {self.scopes[d]})
                self.names[path, self.scopes[d]] = self.vault.scrub_name(path[len(root):], [self.scopes[d]])

    def rel(self, ref, scope):
        """A private file's path within its scope, one part a line: searched for names too (a name can sit
        in a file name only). Files are learned only in prepare(), before the agent starts: learning later
        would take time that depends on the file."""
        node, _, path = ref.partition(":")
        return path[len(max((d.partition(":")[2] for d, s in self.scopes.items() if s == scope and d.startswith(node + ":")
                             and inside(path, d.partition(":")[2])), key=len, default="")):].replace("/", "\n")

    # ---------- the exchange ----------
    def item(self, ref, internal=False):
        """The caller's name for some data -> an item with its scopes. Refusals echo only `asked`."""
        if not self.alias and not internal:
            self.catalog()                                     # the names the caller may use
        ends = [a for a in self.alias if a.endswith("/" + ref.lstrip("/"))] if ref not in self.alias else []
        asked, ref = ref, self.alias.get(ref, self.alias[ends[0]] if len(ends) == 1 else ref)   # a name shown
                                                               # scrubbed (or its tail) means its real file
        internal = internal or asked in self.alias
        try:
            if "\0" in ref:
                raise ValueError
            os.fsencode(ref)
        except (ValueError, UnicodeError):
            raise core.Refused("name files by plain paths") from None
        mine = [r for r in self.refs if r.partition(":")[2] == ref]
        ref = mine[0] if len(mine) == 1 else ref               # a result ref given without its node
        ref = f"{self.host}:{ref}" if ref.startswith("/") else ref
        node, _, path = ref.partition(":")
        if not path.startswith("/"):                           # relative: a file in the agent's workspace
            node, path = self.host, f"{self.ws}/{ref}"
        if node not in self.nodes:
            raise core.Refused(f"unknown node in {asked}")
        path = os.path.normpath(path)                          # ws/../keys is keys
        path = self.upto(path) if node == self.host else path  # /var is /private/var; nothing inside a scope followed
        named = core.label(f"{node}:{path}", self.public, self.scopes)   # as spelled: HR beside hr is not hr on a
        ours = node == self.host and inside(path, self.ws)     # case-sensitive disk (KEYS is keys here: upto spelled it)
        if core.SEALED in named and not ours and f"{node}:{path}" not in self.refs:
            raise self.sealed(asked)                           # from its spelling alone: the disk is never looked at
        named = named if core.SEALED not in named and not ours else None
        if named and not internal and ref not in self.refs and not core.may_hold(self.me, named):
            if not core.may_hold(self.me, core.scrubbed(named)):  # a scope it sees nothing of: decided from the name,
                self.deferred.add(f"{node}:{path}")            # the files are found only inside the job
                return core.Item(f"{node}:{path}", named, node)
            if f"{node}:{path}" not in self.scopes:
                raise core.Refused(f"name files in {sorted(named)} by the names listed (read \".\" lists them)")
        if node == self.host:                                  # upto() followed every link it met by readlink; from
            got = self.kind(path, asked, lambda fd: (os.fstat(fd), getpath(fd) or path))   # here on, none is: one
            if got:                                            # open, no link on the way; its spelling on disk,
                st, path = got                                 # from the open file
                if inside(path, self.ws) and st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
                    raise core.Refused(f"{asked} is hardlinked")
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                    raise core.Refused(f"{asked} is not a regular file")
                self.inodes[f"{node}:{path}"] = (st.st_dev, st.st_ino)
        elif not internal and f"{node}:{path}" not in self.refs and f"{node}:{path}" not in self.snap:
            try:                                               # on another machine: no link on the way there
                transport.kind(self.p["nodes"][node], path)
            except RuntimeError:
                raise core.Refused(f"{asked} goes through a link lego does not follow, or is not a regular file") from None
        ref = f"{node}:{path}"
        scopes = self.refs[ref] if ref in self.refs else core.label(ref, self.public, self.scopes)
        if node == self.host and inside(path, self.ws):
            scopes = frozenset(self.context)                   # the agent's own files hold what it was given
        elif named and core.SEALED in scopes:                 # a scope's own spelling on disk: still the scope
            scopes = named
        roots = [d for d, s in self.scopes.items() if s in scopes and d.startswith(node + ":")]
        if not internal and ref not in self.refs and roots and not any(d == ref for d in roots) and \
                not core.may_hold(self.me, scopes) and core.may_hold(self.me, core.scrubbed(scopes)):
            raise core.Refused(f"name files in {sorted(scopes)} by the names listed (read \".\" lists them)")
        return core.Item(ref, scopes, node)

    def sealed(self, ref):
        return core.Refused(f"{ref} is sealed. You can read files listed under the private directories, files in "
                            f"your workspace {self.ws}, and offload refs as returned. read \".\" lists them.")

    def upto(self, path):
        """The path with links resolved only until it reaches a private directory; the rest as written,
        so naming a path in a scope never looks at what lies inside it. A link is followed only here, by
        readlink, one hop at a time; each step looks up one name in a folder held open, never through a link."""
        cur, rest, hops = "/", [p for p in path.split("/") if p], 0
        while rest:
            if any(inside(cur, r) for r in self.stops):        # a private folder on this disk: never looked into
                if {".", ".."} & set(rest):
                    raise core.Refused("name files inside a private directory by plain paths, without . or ..")
                return os.path.join(cur, *rest)
            p = rest.pop(0)
            if os.path.join(cur, p).casefold() in ("/.vol", "/dev/fd", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
                raise core.Refused("name files by their paths, not by inode or descriptor")
            if p in (".", ".."):
                cur = cur if p == "." else os.path.dirname(cur)
                continue
            try:
                fd = transport.nofollow(cur)
            except OSError:                                    # missing, not a folder, or changed meanwhile:
                return os.path.join(cur, p, *rest)             # as written
            try:
                if stat.S_ISLNK(os.stat(p, dir_fd=fd, follow_symlinks=False).st_mode):
                    if hops == 40:
                        raise core.Refused("too many links")
                    hops, tgt = hops + 1, os.readlink(p, dir_fd=fd)
                    rest = [q for q in os.path.normpath(os.path.join(cur, tgt, *rest)).split("/") if q]
                    cur = "/"                                  # its '..' by name, never by what exists
                else:
                    cur = spelled(fd, p) or os.path.join(cur, p)   # case and firmlinks only: p is no link
            except OSError:
                return os.path.join(cur, p, *rest)
            finally:
                os.close(fd)
        return cur

    def give(self, item, node):
        """The exchange rule: (item as `node` gets it, scrubbed?) or None."""
        if core.may_hold(node, item.scopes):
            return item, False
        if item.ref in self.refs:                              # a model's output: scrubbing cannot clean it
            return None
        s = core.scrubbed(item.scopes)
        return ((core.Item(item.ref, s, item.node), True) if core.may_hold(node, s) else None)

    def bytes_of(self, item, scrubbed, timeout=60, node=None, data=None):
        """An item's bytes, scrubbed if asked. A host file is opened without following links, never waits
        (a FIFO is refused, not opened), and must be the very file that was labelled. `data`: bytes already read."""
        to, (node, _, path) = node or self.me, item.ref.partition(":")
        if data is None and node == self.host:
            fd = transport.nofollow(path)                       # no link anywhere on the way
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise core.Refused("not a regular file")
                if self.inodes.get(item.ref) != (st.st_dev, st.st_ino):
                    raise core.Refused("the file changed after it was labelled")
                data = b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
            finally:
                os.close(fd)
        elif data is None:
            data = self.snap[item.ref] if scrubbed and item.ref in self.snap else transport.read(
                self.p["nodes"][node], path, timeout)
        if scrubbed and not self.mine(item) and not self.vault.known(item.ref, data):
            raise core.Refused("a private file changed or appeared after lego started: restart lego to learn it")
        if not scrubbed:
            return data
        tags = self.tags(item, to)
        made = not self.mine(item) and len(tags) == 1 and (item.ref, *tags) in self.views   # made at the start
        ok, view = self.views[(item.ref, *tags)] if made else self.view(data, tags, self.fresh if self.mine(item) else None)
        if not ok:
            raise core.Refused(view)
        return view

    def view(self, data, tags, fresh=None):
        """(True, scrubbed bytes) or (False, why not)."""
        text = scrub.text_of(data)
        if text is None:
            return False, "not a text file, so it cannot be scrubbed"
        try:
            return True, self.vault.scrub(text, tags, fresh).encode()
        except scrub.Unscrubbable:
            return False, "this file holds identifiers in an encoded form, so it cannot be scrubbed"

    def tags(self, item, node=None):
        """Whose identifiers to replace in an item going to `node`. A private file: its own scopes the
        node does not see raw. What the caller wrote: only scopes it sees raw and the node sees scrubbed (one
        the node sees nothing of is kept out by the context; replacing it would only confirm guesses)."""
        t = (node or self.me).trust
        if self.mine(item):
            return {s for s in self.raw & set(self.scopes.values()) if s + "~" in t and s not in t}
        return {s.rstrip("~") for s in item.scopes if s != core.SEALED} - t   # its own: a view of hr is hr's alone

    def mine(self, item):
        return item.ref == "task" or item.node == self.host and inside(item.ref.partition(":")[2], self.ws)

    def level(self, scope, node=None):
        t = (node or self.me).trust
        return "everything" if scope in t else "scrubbed" if scope + "~" in t else "nothing"

    def catalog(self):
        """The private directories, each at the caller's level: real names, scrubbed names, or a count."""
        lines = []
        for d, s in self.scopes.items():
            node, _, root = d.partition(":")
            lvl = self.level(s)
            found = self.found[d] if (lvl == "nothing" or self.p["nodes"][node].get("ssh")) and d in self.found else \
                transport.files(self.p["nodes"][node], root)           # another machine: as found at the start
            shown = root if node == self.host else d
            hidden = any(self.level(s, self.nodes[n]) == "nothing" for n, v in self.p["nodes"].items() if runnable(v))
            lines.append(f"  {shown}  [{s}]  you see: {lvl}" + {
                "everything": " (names masked as #tags: some model sees nothing of it; read them by tag)" if hidden else "",
                "scrubbed": " (same data; names, emails, phones, addresses, account numbers "
                "and keys replaced by stable placeholders such as [PERSON_3f9a])",
                "nothing": f" ({len(found)} files, names hidden: pass the directory to offload with `returns`)"}[lvl])
            raw = lvl == "everything" and all(s in v.trust for n, v in self.nodes.items() if runnable(self.p["nodes"][n]))
            new, named = 0, []
            for f in (found[:60] if raw else found) if lvl != "nothing" else []:
                if f"{node}:{f}" not in self.learned and not raw and f"{node}:{f}" not in self.vault.v["files"]:
                    new += 1                                   # not learned: its name may hold an identifier
                    continue
                tag = hmac.new(bytes.fromhex(self.vault.v["key"]), f.encode(), "sha256").hexdigest()[:8]
                name = f[len(root):] if raw else self.names[f, s] if (f, s) in self.names else \
                    self.vault.scrub_name(f[len(root):], [s])  # made at the start, when it was learned
                named.append((f"{shown}/#{tag}" if hidden else f"{shown}{name}", tag, f))   # hidden: not even names
                self.learned.add(f"{node}:{f}")
            twice = {n for n, c in collections.Counter(n for n, _, _ in named).items() if c > 1}
            for name, tag, f in named:                         # two names scrubbed alike: each by its own tag,
                self.alias[name + f"#{tag}" * (name in twice)] = f"{node}:{f}"   # never one by raw order
            lines += sorted("    " + n + f"#{t}" * (n in twice) for n, t, _ in named)[:60]   # the raw order is private
            if new:
                lines.append(f"    ({new} files added since lego started: restart lego to use them)")
        lines += [f"  {r}" for r in self.p.get("catalog", [])]
        return "\n".join(lines) or "  (none)"

    def listing(self):
        files = sorted(os.path.relpath(f, self.ws) for f in transport.walk(self.ws, lambda e: True, lambda n: n.startswith(".")))
        return (f"Your workspace {self.ws} (relative paths mean files here):\n" +
                "\n".join("  " + f for f in files[:200]) + "\nPrivate directories:\n" + self.catalog())

    def read(self, ref):
        it = self.item(ref.strip() or ".")
        path = it.ref.partition(":")[2]
        if any(inside(d.partition(":")[2], path) for d in self.scopes) or it.ref == f"{self.host}:{self.ws}":
            return {"row": "listing", "text": self.listing()}  # what you can name, at your level
        if core.SEALED in it.scopes:
            raise self.sealed(ref)
        got = self.give(it, self.me)
        if not got:
            raise core.Refused(f"you may not see {sorted(it.scopes)}; ask about it with offload and `returns`")
        kind = self.kind(path) if it.node == self.host else stat.S_IFREG
        if kind == stat.S_IFDIR:                               # only once the level allows it
            return {"row": "listing", "text": self.listing()}
        if kind != stat.S_IFREG:
            raise core.Refused(f"no file {ref}" + (" in your workspace; private files go by the full paths listed under "
                               "Private directories" if not ref.startswith("/") and ref.partition(":")[0] not in self.nodes
                               else "") + ". read \".\" lists what you can name.")
        text, before = self.bytes_of(*got).decode(errors="replace"), self.cut_off()
        self.taint(got[0].scopes)
        self._note(op="read", ref=it.ref, scopes=sorted(got[0].scopes), scrubbed=got[1])
        cut = [n for n in self.cut_off() if n not in before]   # from the scopes and the policy alone
        return {"row": "scrubbed" if got[1] else "full", "text": text,
                **({"note": f"lego: {', '.join(cut)} can now take no more tasks from you."} if cut else {})}

    def kind(self, path, asked="the path", of=lambda fd: stat.S_IFMT(os.fstat(fd).st_mode)):
        """of(a host path opened with no link followed on the way), by default its file type (None: missing)."""
        try:
            fd = transport.nofollow(path)
        except FileNotFoundError:
            return None
        except OSError:                                        # a link in a private folder, or one swapped in after
            raise core.Refused(f"{asked} goes through a link lego does not follow; name the file itself") from None
        try:
            return of(fd)
        finally:
            os.close(fd)

    def cut_off(self):
        """The other models that may not be given what the caller's context holds."""
        return [n for n, v in self.p["nodes"].items() if runnable(v) and n != self.caller and any(
            self.level(s.rstrip("~"), self.nodes[n]) == "nothing" for s in self.context if s != core.SEALED)]

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
        block = {n: sorted(s for s in need - {core.SEALED} if self.level(s.rstrip("~"), self.nodes[n]) == "nothing")
                 for n in names}
        why = "; ".join(f"{n} sees nothing of {', '.join(b)}" for n, b in block.items() if b)
        held = (f". Your context holds {', '.join(sorted(s for s in self.context if '~' not in s))} from what you read, "
                "and it goes with every task: do the rest yourself, or start a new workspace") if self.context else ""
        raise core.Refused(f"no model may be given {sorted(need)}" + (f" ({why})" if why else "") + held)

    def decide(self, n, items, returns, t):
        """Everything about a call, from labels, the ledger and node n's queue at time t, before any
        byte is read: the output item, its row, the ledger after, when the job may start and when it
        must end. The bricks see only the scopes hidden from the caller."""
        need = frozenset().union(*(i.scopes for i in items))
        extra, start = core.queue(self.slots[n], t, self.hidden(need))
        out = core.Item(f"{n}:{self.p['nodes'][n]['out']}/{secrets.token_hex(16)}.txt", need | extra, n)
        with self.ledger_file.open("r+") as f:                 # charged under the lock, before any byte is read
            fcntl.flock(f, fcntl.LOCK_EX)
            row, ledger = core.release_plan(core.Item(out.ref, self.hidden(out.scopes), n), returns, json.load(f))
            f.seek(0), f.truncate(), json.dump(ledger, f)
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
            if it.ref in self.deferred:
                out.append(it)
                continue
            if any(inside(r, path) and r != path for r in self.roots):   # a folder holding a private one: named
                raise core.Refused(f"name the private directories in {r} one by one")        # one by one, unwalked
            if node == self.host and inside(path, self.ws) and self.kind(path) is None:   # its own files only
                raise core.Refused(f"no file {r} in your workspace. read \".\" lists what you can name.")
            found = [] if it.ref in self.refs or core.SEALED in it.scopes else self.listed(node, path)
            got = [it]
            if found and found != [path]:                      # one swapped for a link meanwhile: dropped, whatever
                got = []                                       # it points at, and before a look at it
                for f in (f for f in found if inside(f, path)):
                    with contextlib.suppress(core.Refused):
                        got.append(self.item(f"{node}:{f}", internal=True))
            out += [i for i in got if inside(i.ref.partition(":")[2], path)]   # swapped for a link meanwhile: dropped
        return out

    def offload(self, task, inputs=(), returns=None, node=None):
        if returns is not None:
            core.bits(returns)                                 # refuse bad shapes before anything else
        ins = self.items(inputs) + [core.Item("task", frozenset(self.context), self.host)]   # the task text is last
        # a job on what the caller may see comes back in full; otherwise on the raw inputs, shaped
        views = [self.give(i, self.me) for i in ins]
        srcs = views if returns is None and all(views) else [(i, False) for i in ins]
        n = self.pick_node([i for i, _ in srcs], node)
        everything = set(self.scopes.values()) <= self.nodes[n].trust
        reader = core.Node(n, self.nodes[n].trust & self.seen) if srcs is views else self.nodes[n]   # who sees the
        given = [(g, s or s2 or (self.mine(i) and not everything and bool(self.tags(i, reader))))   # what the caller wrote:
                 for (i, s), (g, s2) in zip(srcs, (self.give(i, self.nodes[n]) for i, _ in srcs))]  # like its task
        if any(self.whole(i, s, n) and i.node not in (self.host, n) for i, s in given):
            raise core.Refused(f"data hidden from you on another machine goes only to a model on that machine, not {n}")
        if any(i.ref in self.refs and i.node != n and not core.may_hold(self.me, i.scopes) for i, _ in given[:-1]):
            raise core.Refused(f"a job's output you may not see stays on its machine: run the next job there, not {n}")
        if any(s and i.ref in self.deferred for i, s in given):
            raise core.Refused("data hidden from you goes only to a model that sees it raw")
        scrub_task = given[-1][1] or not everything            # names it saw in this tool's text, too
        tags = self.tags(ins[-1], reader) if scrub_task else set()
        task = self.vault.scrub(task, tags, self.fresh) if scrub_task else task
        if returns and tags:                                   # its literals reach the model as written
            for x in literals(returns):
                if self.vault.own(scrub.norm(str(x)), {}, tags) != scrub.norm(str(x)):
                    raise core.Refused(f"returns: {x!r} would reach {n} as written, but it sees this data scrubbed. "
                                       "Use plain words, placeholders you were given, and int bounds under 1000")
        typed = set() if tags else self.raw & set(self.scopes.values())   # a task that went raw: its output may hold them
        snap = {i.ref: self.bytes_of(i, False) for i, _ in given[:-1] if self.mine(i)}   # the caller's files, read
        with self.locks[n]:                                    # now: the job never touches the workspace
            t0 = time.time()
            out, row, ledger, start, end = self.decide(n, [g for g, _ in given], returns, t0)
            self._note(op="offload", node=n, ref=out.ref, inputs=[i.ref for i in ins[:-1]], scopes=sorted(out.scopes),
                       scrubbed=[s for _, s in given], row=row, bits=core.bits(returns) if row == "shaped" else 0)
            self.refs[out.ref] = out.scopes | typed
            turn, self.tickets[n] = self.tickets[n], self.tickets[n] + 1
            run = lambda: self._run(n, given[:-1], task, returns, out, row, end, turn, everything, reader, snap)  # noqa: E731
            if row == "full":                                  # the caller may see it: the node is surely idle
                time.sleep(max(0.0, start - time.time()))
                r = run()
                self.slots[n] = (time.time(), frozenset())
                output, raw = r.get("output"), {s.rstrip("~") for s in out.scopes} & self.seen
                if output and (scrub_task or any(s for _, s in given)):
                    output = self.vault.unscrub(output, raw, self.fresh)   # this job's scopes seen raw, its own words
                self.taint(out.scopes | raw)
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
        value = core.clock(result.get("value", core.UNKNOWN), 0 if not job.is_alive() else deadline + 1, deadline)
        self._note(op="answer", ref=out.ref, answer=value)
        return {"row": "shaped", "answer": value, "ref": out.ref, "allowance_left": ledger}

    def _run(self, n, given, task, returns, out, row, end, turn, everything, reader, snap):
        """One job on node n. Staging and the model call both stop by `end`, the bound its slot was
        booked with, so no later job's timing depends on how long this one would have run."""
        node = self.p["nodes"][n]
        def left():
            if time.time() >= end:
                raise TimeoutError("the job reached its bound")
            return end - time.time()
        with self.turns[n]:                                    # first in, first out: one job at a time
            self.turns[n].wait_for(lambda: self.serving[n] == turn)
        try:
            files = []
            if any(s for _, s in given) or not everything:    # the real names' order is theirs too
                given = secrets.SystemRandom().sample(given, len(given))
            for i, s in given:
                if self.whole(i, s, n):                        # raw and not the caller's to see: its whole scope,
                    # as one pipe, walked on the node, where a link on the way gives no file (keys/mirror -> hr);
                    # nothing here looks inside it, and no single file's size or name crosses
                    root = next(r for d in self.scopes if (r := d.partition(":")[2]) and d.startswith(i.node + ":")
                                and inside(i.ref.partition(":")[2], r))
                    if i.node != n and (n, i.node, root) not in self.staged:   # the whole scope, once per node,
                        self.staged[n, i.node, root] = transport.put_tree(node, node["stage"], root, left())   # from
                    files.append(i.ref.partition(":")[2] if i.node == n else   # this disk: the wire is the same
                                 self.staged[n, i.node, root] + i.ref.partition(":")[2][len(root):])   # whatever was named
                    continue
                name = i.ref.rsplit("/", 1)[-1]
                name = name if everything and not s else self.vault.scrub_name(
                    name, self.tags(i, reader), self.fresh if self.mine(i) else None)
                files.append(i.ref.partition(":")[2] if i.node == n and not s else bounded(lambda: transport.put(
                    node, node["stage"], name, self.bytes_of(i, s, left(), reader, snap.get(i.ref)), left()), left()))
            core.run([core.move(g, self.nodes[n]) for g, _ in given], self.nodes[n], out.ref)
            r = transport.run_job(node, files, task, returns, out.ref.partition(":")[2], keep=row == "receipt",
                                  timeout=left(), salt=core.salt(self.salt_key, out.scopes), shaped=row == "shaped")
            self._note(op="done", ref=out.ref, took=round(r["took"], 2))
            if row == "shaped":                                # here, before `end`: the reply never parses
                o = r.get("output")
                r["value"] = core.shape(r["answer"], returns) if "answer" in r else core.shape(transport.answer(o), returns) \
                    if isinstance(o, str) and len(o) <= 4096 else core.UNKNOWN
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
                "and `offload` tools can, at the level shown.\nPrivate directories (name files by these full paths):\n" +
                self.catalog() +
                f"\nYour workspace: {self.ws}. Relative paths mean files there. Anything else is sealed.\n"
                "Other models: " + ("; ".join(f"{n} (sees " + ", ".join(f"{s}: {self.level(s, self.nodes[n])}"
                                                                       for s in kinds) + ")" for n in others) or "none") +
                ".\nWhat you read stays in your context: every task you offload from this workspace carries it, now "
                "and in later sessions, and a model that may not see it refuses the task. Read only what the task needs" +
                "".join(f"; reading {s} means {', '.join(n for n in others if self.level(s, self.nodes[n]) == 'nothing')} "
                        f"can take no more tasks from you, so offload to it first" for s in kinds if self.level(s) != "nothing" and
                        any(self.level(s, self.nodes[n]) == "nothing" for n in others)) + "." +
                (f" Your context already holds {', '.join(held)}." if (held := sorted(
                    {s.rstrip("~") for s in self.context} - {core.SEALED} - {s for s in kinds if self.level(s) == "nothing"})) else "") + "\n"
                "`offload` gives another model your task text and the `inputs` files, nothing else: write complete "
                "instructions and list every file (a directory stands for all its files). If you could see all the "
                "inputs, the model gets them at your level (or its own, if lower) and you get the full result, with "
                "placeholders of data you see raw put back: tell it to copy placeholders as written. To use more than "
                "you see, pass `returns`: one small typed answer paid from a per-scope bit allowance and sent after a "
                "fixed delay; without it you get a receipt (the result is saved for the user; its ref can be an input later).")

    def tools(self):
        return [{"name": "read", "description": "Read a file at your level: raw, scrubbed, or refused. `read \".\"` "
                 "lists your workspace and the private files you can name.\n\n" + self.about(), "inputSchema": {
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
                if isinstance(a, dict) and isinstance(a.get("inputs"), str):      # one path, or a list sent as text
                    with contextlib.suppress(ValueError):
                        a["inputs"] = json.loads(a["inputs"]) if a["inputs"].lstrip().startswith("[") else [a["inputs"]]
                if not isinstance(a, dict) or not isinstance(a.get("inputs") or [], list):
                    raise core.Refused("arguments must be {task, inputs: [..], returns, node}")
                if name == "read":
                    out = self.read(str(a.get("path") or a.get("file_path") or ""))
                else:
                    out = self.offload(str(a.get("task", "")), a.get("inputs") or [], a.get("returns"), a.get("node"))
            except Exception as e:                             # fail closed: a call that breaks gets nothing
                why = str(e) if isinstance(e, core.Refused) else "call failed"
                self._note(op="refused", reason=why)
                out = {"refused": why}
            note = out.pop("note", None)                       # a read that cut a model off says so
            res = {"content": [{"type": "text", "text": out.pop("text") if "text" in out else json.dumps(out)}] +
                   ([{"type": "text", "text": note}] if note else []), "isError": "refused" in out}
        elif mid is None:
            return None                                        # notification
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {m}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(door, port, bind="127.0.0.1", token=None, peers=None):
    """MCP over HTTP. With `token`, only a request with `Authorization: Bearer <token>` from an address in
    `peers` (the VM network) is answered."""
    class H(BaseHTTPRequestHandler):
        log_message = lambda self, *a: None  # noqa: E731

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            if token and (self.headers.get("Authorization") != f"Bearer {token}" or peers is not None and
                          ipaddress.ip_address(self.client_address[0]) not in peers):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                reply = door.rpc(msg) if isinstance(msg, dict) else None
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad request"}}
            body = json.dumps(reply).encode() if reply else b""
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):   # the agent left before its reply
                self.send_response(200 if reply else 202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    srv = ThreadingHTTPServer((bind, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
