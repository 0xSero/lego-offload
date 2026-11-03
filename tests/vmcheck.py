"""Every connection of a lego session, checked from inside the agent VM, and what the VM must not reach.

    python3 tests/vmcheck.py

The door and the route run as in a real session (synthetic data, the e2e policy), and the VM's
home is the one the agent adapters write; the VM runs a shell script instead of an agent. Also
checked on the Mac: no credential the route injects appears in that home, an adapter's argv or its
environment (compared in memory, never printed). Prints one line per check, non-zero on failure.
"""
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import door  # noqa: E402
import lego  # noqa: E402
import route  # noqa: E402
import agents  # noqa: E402
import e2e  # noqa: E402
import vm  # noqa: E402

SCRIPT = r'''
check() { if eval "$2" >/dev/null 2>&1; then echo "ok   $1"; else echo "FAIL $1"; fi; }
mcp() { curl -s -m 20 -X POST "$DOOR" -H "Content-Type: application/json" -H "Authorization: Bearer $1" -d "$2"; }
check "door answers with the session key"        'mcp "$KEY" "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}" | grep -q offload'
check "door refuses a wrong key"                  '[ "$(curl -s -o /dev/null -w %{http_code} -m 10 -X POST "$DOOR" -H "Authorization: Bearer nope" -d {})" = 403 ]'
check "door lists the private folders by level"   'mcp "$KEY" "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"read\",\"arguments\":{\"path\":\".\"}}}" | grep -q "you see: nothing"'
check "route reaches Anthropic with the key"      'curl -s -m 60 "$ROUTE/v1/messages" -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d "{\"model\":\"claude-sonnet-5-5\",\"max_tokens\":64000,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" | grep -qE 'request_id|"id": *"msg_''
check "route refuses a wrong key"                 '[ "$(curl -s -o /dev/null -w %{http_code} -m 10 "$ROUTE/v1/messages" -H "x-api-key: nope" -d {})" != 200 ]'
check "workspace is mounted and writable"         'touch "$WS/.vmcheck" && rm "$WS/.vmcheck"'
check "private folders are not in the VM"         '! ls "$PRIVATE"'
check "the Mac home is not in the VM"             '! ls "$MACHOME/.ssh"'
check "no ssh key in the VM"                      '! ls ~/.ssh/id_*'
check "no Claude login in the VM"                 '! ls ~/.claude/.credentials.json'
check "claude is installed"                       'claude --version'
check "omp is installed"                          'omp --version'
'''


def main():
    root = Path(tempfile.mkdtemp(prefix="lego-vmcheck-"))
    os.environ["LEGO_TAINT"] = str(root / "taint.json")
    simple, _ = e2e.make(root)
    simple["models"]["cloud"]["sees"] = "nothing"
    state = root / ".lego"
    policy = door.expand(simple, state)
    d = door.Door(policy, state, "cloud")
    d.prepare(lego.finder(policy), strict=True)
    vm.ensure_image()
    gw, net = vm.network()
    key = secrets.token_hex(24)
    ds = door.serve(d, 0, bind="0.0.0.0", token=key, peers=net)
    rs = route.serve(0, "anthropic", bind="0.0.0.0", token=key, peers=net)
    home = tempfile.mkdtemp(prefix="lego-home-")
    try:
        door_url, route_url = f"http://{gw}:{ds.server_address[1]}/mcp", f"http://{gw}:{rs.server_address[1]}"
        wrote = [agents.claude(home, route_url, door_url, ["-p", "hi"], policy["workspace"], key=key),
                 agents.omp(home, route_url, door_url, ["-p", "hi"], policy["workspace"], key=key,
                            model={"model": "local", "url": "http://127.0.0.1:9/v1"})]
        blob = "\n".join([x for argv, env in wrote for x in (*argv, *env.values())] +
                         [f.read_text(errors="replace") for f in Path(home).rglob("*") if f.is_file()])
        env = {"KEY": key, "DOOR": door_url, "ROUTE": route_url,
               "WS": policy["workspace"], "PRIVATE": str(root / "private"), "MACHOME": os.path.expanduser("~")}
        r = vm.launch(["bash", "-c", SCRIPT], policy["workspace"], home, env=env, timeout=600)
        out = r.stdout + r.stderr
        print(out.strip())
        here = [("door refuses a wrong key from this Mac too", route_refused(ds.server_address[1])),
                ("no Mac credential in the agents' home, argv or env", not any(c in blob for c in credentials())),
                ("the agents' model is the route and their tool the door", route_url in blob and door_url in blob
                 and "api.anthropic.com" not in blob)]
        for name, ok in here:
            print(f"{'ok  ' if ok else 'FAIL'} {name}")
        fails = out.count("FAIL") + sum(not ok for _, ok in here)
        print(f"VMCHECK {'ok' if not fails else f'{fails} failed'}")
        return 1 if fails else 0
    finally:
        for s in (ds, rs):
            s.shutdown(), s.server_close()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def credentials():
    """Every credential the route may inject (the part that makes it valid)."""
    out = []
    for _, _, auth, _ in route.UPSTREAMS.values():
        try:
            out += [v.split(" ", 1)[-1] for v in auth().values()]
        except Exception:                                       # not logged in here
            pass
    return [c for c in out if len(c) >= 16]


def route_refused(port):
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), headers={"Authorization": "Bearer nope"}), timeout=10)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 403


if __name__ == "__main__":
    sys.exit(main())
