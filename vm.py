"""VM: runs a coding agent in a fresh Linux VM (Apple `container`, one per session). Inside it has
its workspace and a throwaway home, both mounted at their paths on the Mac, and nothing else of
the Mac: it reaches the Mac only at the route (its model) and the door (the offload tool), and
each answers only callers from the VM network that hold this session's token.
"""
import ipaddress
import json
import os
import subprocess
from pathlib import Path

IMAGE = "lego-agent:1"
DOCKERFILE = Path(__file__).resolve().parent / "vm"


def _run(*args, timeout=60):
    return subprocess.run(["container", *args], capture_output=True, text=True, timeout=timeout)


def network():
    """(the Mac's address as the VM sees it, the VM network): the service is started if it is not."""
    if _run("system", "status", timeout=20).returncode:
        _run("system", "start", "--enable-kernel-install", timeout=300)
    net = json.loads(_run("network", "inspect", "default", timeout=20).stdout)[0]["status"]
    return net["ipv4Gateway"], ipaddress.ip_network(net["ipv4Subnet"])


def ensure_image():
    if _run("image", "inspect", IMAGE, timeout=30).returncode:
        print(f"lego: building the agent VM image {IMAGE} (once, a few minutes)", flush=True)
        subprocess.run(["container", "build", "-t", IMAGE, str(DOCKERFILE)], check=True, timeout=3600)


def launch(argv, ws, home, env=(), timeout=900, tty=False):
    """Run `argv` in a fresh VM. With tty the agent gets this terminal; otherwise stdin is closed and
    stdout/stderr come back as text in the CompletedProcess."""
    given, given_home = os.path.abspath(ws), os.path.abspath(home)
    ws, home = os.path.realpath(ws), os.path.realpath(home)
    os.makedirs(f"{home}/tmp", exist_ok=True)
    e = {"HOME": home, "TMPDIR": f"{home}/tmp", "LANG": "C.UTF-8", "IS_SANDBOX": "1", "TRACK_EXEMPT": "1",
         "TERM": os.environ.get("TERM", "xterm-256color") if tty else "dumb", **dict(env)}
    spelled = {(p, q) for p, q in ((ws, ws), (home, home), (ws, given), (home, given_home))}   # /var and /private/var
    cmd = ["container", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-w", ws,
           *[x for p, q in sorted(spelled) for x in ("-v", f"{p}:{q}")], *(["-it"] if tty else []),
           *[x for k, v in e.items() for x in ("-e", f"{k}={v}")], IMAGE, *argv]
    pipes = {} if tty else {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
    return subprocess.run(cmd, timeout=timeout, **pipes)
