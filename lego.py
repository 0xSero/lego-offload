"""lego: run a cloud coding agent in the room, with private work offloaded through the door.

usage: lego [--policy FILE] AGENT [agent args...]   AGENT: claude | codex | cursor | cline
       lego [--policy FILE] reset                  refill every scope's allowance
       lego [--policy FILE] app [PORT]             the web app (default port 8470)
       lego app --demo [PORT]                      the web app on synthetic data (gpu-box)

  lego claude --dangerously-skip-permissions
  lego codex --dangerously-bypass-approvals-and-sandbox
  lego cursor -p "How many people earn over 100k?" --yolo
  lego cline -y "Is the STRIPE_KEY a live key?"

The policy (default: $LEGO_POLICY, ./lego.json, then ~/.lego/policy.json; see
examples/policy.json) names the nodes, what each may hold, which directories carry which
scope, the public files, the allowances and the reply deadline. The agent's workspace is
the policy's "workspace", or the current directory. State lives in .lego/ next to the policy.
"""
import fcntl
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import agents
import door
import room
import route


def session(policy, state, agent, args, capture=None, timeout=None, tty=False):
    """One agent session: start the door and the route on free ports, run the agent in the room, stop both."""
    adapter, upstream = agents.AGENTS[agent]
    Path(state).mkdir(parents=True, exist_ok=True)
    lock = open(Path(state) / "session.lock", "w")             # one door per node: its queue must see every job
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("lego: another session is using this policy's nodes; run one at a time")
    d = door.Door(policy, state)
    ds, rs = door.serve(d, 0), route.serve(0, upstream, capture=capture)
    dp, rp = ds.server_address[1], rs.server_address[1]
    home = tempfile.mkdtemp(prefix="lego-home-")
    try:
        argv, install, env = adapter(home, f"http://127.0.0.1:{rp}", f"http://127.0.0.1:{dp}/mcp", list(args),
                                     policy["workspace"])
        return room.launch(argv[0], argv[1:], policy["workspace"], home, rp, dp, env=env, timeout=timeout,
                           agent_dir=install, tty=tty)
    finally:
        for s in (ds, rs):
            s.shutdown(), s.server_close()
        shutil.rmtree(home, ignore_errors=True)
        for job in d.jobs:                                     # the node is busy until its jobs end
            job.join()
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
    policy = json.loads(path.read_text())
    policy.setdefault("workspace", os.getcwd())
    state = path.resolve().parent / ".lego"
    cmd, args = argv[0], argv[1:]
    if cmd == "reset":
        return door.Door(policy, state).reset()
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
