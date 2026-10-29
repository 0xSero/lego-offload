"""lego: run a cloud coding agent in the room, with private work offloaded through the door.

usage: python3 lego.py POLICY.json [claude args...]     e.g. -p "How many ...?" --output-format json
       python3 lego.py POLICY.json --reset              refill every scope's allowance

The policy (see tests/e2e.py for a full one) names the nodes, what each may hold, which
directories carry which scope, the public files, the allowances and the reply deadline.
State (ledger, door log) lives in .lego/ next to the policy.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import door
import room
import route

ROUTE_PORT, DOOR_PORT = 18471, 18472
TOOLS = "mcp__lego__offload,Bash,Read,Write,Edit,Glob,Grep"


def session(policy, state, args, capture=None, agent=None, timeout=900):
    """One agent session: start the door and the route, run the agent in the room, stop both."""
    d = door.Door(policy, state)
    servers = [door.serve(d, DOOR_PORT), route.serve(ROUTE_PORT, capture=capture)]
    home = tempfile.mkdtemp(prefix="lego-home-")
    try:
        mcp = Path(home, "mcp.json")
        mcp.write_text(json.dumps({"mcpServers": {"lego": {"type": "http", "url": f"http://127.0.0.1:{DOOR_PORT}/mcp"}}}))
        agent = agent or shutil.which("claude")
        return room.launch(agent, [*args, "--mcp-config", str(mcp), "--strict-mcp-config", "--allowedTools", TOOLS],
                           policy["workspace"], home, ROUTE_PORT, DOOR_PORT, timeout=timeout)
    finally:
        for s in servers:
            s.shutdown(), s.server_close()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    policy = json.loads(Path(sys.argv[1]).read_text())
    state = Path(sys.argv[1]).resolve().parent / ".lego"
    if sys.argv[2] == "--reset":
        door.Door(policy, state).reset()
        sys.exit(0)
    r = session(policy, state, sys.argv[2:])
    sys.stdout.write(r.stdout), sys.stderr.write(r.stderr)
    sys.exit(r.returncode)
