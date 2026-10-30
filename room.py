"""Room: runs a coding agent under room/room.sb (deny by default). Inside it can touch its
workspace, a throwaway HOME and the agent's own install directory, and reach two localhost
ports: the Route (its model) and the Door (the offload tool). No ssh, no DNS, no keychain,
no other files.
"""
import os
import subprocess
from pathlib import Path

PROFILE = Path(__file__).resolve().parent / "room" / "room.sb"


def check(*dirs):
    """The profile matches paths, so a hardlink planted before launch would be a second name
    for an outside file. Refuse any regular file with more than one link."""
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                p = os.path.join(root, f)
                if not os.path.islink(p) and os.stat(p).st_nlink > 1:
                    raise PermissionError(f"room: hardlinked file {p}")


def launch(agent, args, ws, home, route_port, door_port, env=(), timeout=900, agent_dir=None, tty=False, lib_dir=None):
    """Run `agent args...` in the room. With tty the agent gets this terminal (interactive use);
    otherwise stdin is closed and stdout/stderr come back as text in the CompletedProcess.
    lib_dir: one more read-only directory of the agent's own code (omp's native modules)."""
    ws, home = os.path.realpath(ws), os.path.realpath(home)    # the sandbox sees real paths
    agent = os.path.realpath(agent)
    agent_dir = os.path.realpath(agent_dir or os.path.dirname(agent))
    lib_dir = os.path.realpath(lib_dir or agent_dir)
    os.makedirs(f"{home}/tmp", exist_ok=True)
    check(ws, home)
    e = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "TMPDIR": f"{home}/tmp", "USER": os.environ["USER"],
         "LANG": "en_US.UTF-8", "TERM": os.environ.get("TERM", "xterm-256color") if tty else "dumb",
         "SHELL": "/bin/bash", "TRACK_EXEMPT": "1", **dict(env)}
    sb = ["/usr/bin/sandbox-exec", "-f", str(PROFILE), "-D", f"WS={ws}", "-D", f"HOME_DIR={home}",
          "-D", f"AGENT_DIR={agent_dir}", "-D", f"LIB_DIR={lib_dir}", "-D", f"ROUTE_PORT={route_port}", "-D", f"DOOR_PORT={door_port}"]
    if tty:
        return subprocess.run(sb + [agent, *args], cwd=ws, env=e, timeout=timeout)
    return subprocess.run(sb + [agent, *args], cwd=ws, env=e, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)
