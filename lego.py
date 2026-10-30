"""lego: run a coding agent in a fresh VM that holds nothing of this Mac but its workspace. Its one
way to your private data is the lego tool, which answers from this Mac at each model's level.

usage: lego [--policy FILE] AGENT [agent args...]   AGENT: claude | omp
       lego [--policy FILE] reset                  refill every scope's allowance
       lego [--policy FILE] reset --workspace      forget what earlier sessions put in the workspace
       lego [--policy FILE] show REF               print a job's output (a ref the agent got back)
       lego [--policy FILE] app [PORT]             the web app (default port 8470)
       lego app --demo [PORT]                      the web app on synthetic data (gpu-box)

  lego claude --dangerously-skip-permissions       Claude in front, the local model as its helper
  lego claude -p "How many people earn over 100k?"
  lego omp                                         a local model in front, the cloud as its helper

Run it from the folder the agent should work in (its workspace). The VM image is built the first
time (Apple `container`, a few minutes).

The policy (default: $LEGO_POLICY, ./lego.json, then ~/.lego/policy.json; see
examples/policy.json) names the private directories and, for each model, what it sees of
them: "everything", "scrubbed" (identifiers swapped for placeholders) or "nothing". Cloud
agents run as the model with "api"; omp runs as the first model with a "url". The agent's
workspace is the policy's "workspace", or the current directory. State lives in .lego/ next
to the policy.
"""
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import agents
import door
import route
import transport
import vm


def caller(policy, agent):
    """The node whose model the agent talks to: a local model for omp, else the node with the agent's own api."""
    up = agents.AGENTS[agent][1]
    for n, v in policy["nodes"].items():
        m = v.get("model") or {}
        if (m.get("url") if up is None else m.get("api") == up):
            return n
    if up is None:
        raise SystemExit(f"lego: {agent} needs a model with a \"url\" in the policy")
    return None


def finder(policy):
    """For the vault: scope -> a function listing a file's identifiers, run by a local model that sees the scope raw."""
    def find(scope):
        for v in policy["nodes"].values():
            if (v.get("model") or {}).get("url") and scope in v["trust"]:
                return lambda text: transport.find_ids(v, text)
    return find


def session(policy, state, agent, args, capture=None, timeout=None, tty=False):
    """One agent session: start the door and the route on free ports, run the agent in its VM, stop both."""
    adapter, upstream = agents.AGENTS[agent]
    me = caller(policy, agent)
    model = policy["nodes"][me]["model"] if me else None
    upstream = upstream or model["url"].removesuffix("/v1")
    Path(state).mkdir(parents=True, exist_ok=True)
    locks = Path("~/.lego/locks").expanduser()
    locks.mkdir(parents=True, exist_ok=True)
    ws, up = policy["workspace"], []
    while ws not in up:
        up.append(ws)
        ws = os.path.dirname(ws)
    key = lambda d: locks / f"{hashlib.sha256(d.encode()).hexdigest()[:16]}.lock"  # noqa: E731
    held = [(open(Path(state) / "session.lock", "w"), fcntl.LOCK_EX), (open(key(up[0]), "w"), fcntl.LOCK_EX)] + [
        (open(key(d), "w"), fcntl.LOCK_SH) for d in up[1:]]
    try:                                                       # one door per node (its queue sees every job),
        for lock, how in held:                                 # one session per workspace, nested ones included:
            fcntl.flock(lock, how | fcntl.LOCK_NB)             # yours exclusive, every parent shared
    except BlockingIOError:
        raise SystemExit("lego: another session is using this policy's nodes or this workspace; run one at a time")
    d = door.Door(policy, state, me)
    if not d.held <= d.seen:
        raise SystemExit(f"lego: the workspace holds {sorted(d.held - d.seen)} data from an earlier session, which "
                         f"{agent}'s model may not see. Use another workspace, or clear it and run: lego reset --workspace")
    d.prepare(finder(policy), strict=True)
    vm.ensure_image()
    gw, net = vm.network()
    key = secrets.token_hex(24)                                # the session's key to the route and the door
    ds = door.serve(d, 0, bind="0.0.0.0", token=key, peers=net)
    rs = route.serve(0, upstream, capture=capture, bind="0.0.0.0", token=key, peers=net)
    dp, rp = ds.server_address[1], rs.server_address[1]
    home = tempfile.mkdtemp(prefix="lego-home-")
    try:
        argv, env = adapter(home, f"http://{gw}:{rp}", f"http://{gw}:{dp}/mcp", list(args), policy["workspace"],
                            key=key, **({"model": model} if agent == "omp" else {}))
        return vm.launch(argv, policy["workspace"], home, env=env, timeout=timeout, tty=tty)
    finally:
        for s in (ds, rs):
            s.shutdown(), s.server_close()
        shutil.rmtree(home, ignore_errors=True)
        for job in d.jobs:                                     # the node is busy until its jobs end
            job.join()
        for lock, _ in held:
            lock.close()


def find_policy(argv):
    if argv[:1] == ["--policy"]:
        return Path(argv[1]), argv[2:]
    for p in (os.environ.get("LEGO_POLICY"), "lego.json", "~/.lego/policy.json"):
        if p and Path(p).expanduser().exists():
            return Path(p).expanduser(), argv
    sys.exit("lego: no policy found (pass --policy FILE, set LEGO_POLICY, or create ./lego.json)")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if argv[:2] == ["app", "--demo"]:
        import app
        return app.demo(int(argv[2]) if len(argv) > 2 else 8470)
    path, argv = find_policy(argv)
    state = path.resolve().parent / ".lego"
    policy = door.expand(json.loads(path.read_text()), state)
    policy["workspace"] = door.canon(policy.get("workspace") or os.getcwd())   # one spelling, for the VM and labels
    cmd, args = argv[0], argv[1:]
    if cmd == "reset":
        d = door.Door(policy, state)
        if args == ["--workspace"]:                            # you vouch that it holds no private data now
            t = {w: v for w, v in d._taints().items() if not door.inside(door.canon(w.lstrip("@")), d.ws)}   # and folders in it
            return d.taint_file.write_text(json.dumps(t))
        return d.reset()
    if cmd == "show":                                          # for you, on the Mac: never for the agent
        node, _, out = (args[0] if args else "").partition(":")
        if node not in policy["nodes"] or not out.startswith(policy["nodes"][node].get("out", "\0") + "/") or "/.." in out:
            sys.exit("lego: show takes a job's ref, such as pop:/srv/lego/out/<id>.txt")
        return sys.stdout.write(transport.read(policy["nodes"][node], out).decode(errors="replace"))
    if cmd == "app":
        import app
        return app.main(policy, state, int(args[0]) if args else 8470)
    if cmd not in agents.AGENTS:
        sys.exit(f"lego: unknown agent {cmd!r}; choose one of {', '.join(agents.AGENTS)}")
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    r = session(policy, state, cmd, args, tty=tty)
    if not tty:
        sys.stdout.write(r.stdout), sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
