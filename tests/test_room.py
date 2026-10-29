"""The room without a model: plain binaries launched exactly as the agent is.
Inside: the workspace and the room HOME. Outside: everything else, including the network."""
import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import room  # noqa: E402

base = Path(tempfile.mkdtemp(prefix="lego-room-test-")).resolve()
ws, home, outside = base / "ws", base / "home", base / "outside"
for d in (ws, home, outside):
    d.mkdir()
(ws / "README.md").write_text("public\n")
(outside / "secret.txt").write_text("private\n")
real_home = os.path.expanduser("~")
other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)   # any other
threading.Thread(target=other.serve_forever, daemon=True).start()                                 # local service


def inside(cmd, *args):
    return room.launch(cmd, list(args), ws, home, 18471, 18472, timeout=30).returncode == 0


cases = [
    ("read workspace file", True, inside("/bin/cat", "README.md")),
    ("write workspace file", True, inside("/usr/bin/touch", f"{ws}/new.txt")),
    ("read file outside workspace", False, inside("/bin/cat", f"{outside}/secret.txt")),
    ("write outside workspace", False, inside("/usr/bin/touch", f"{outside}/x")),
    ("list the real home", False, inside("/bin/ls", real_home)),
    ("read ~/.ssh", False, inside("/bin/ls", f"{real_home}/.ssh")),
    ("read the keychain", False, inside("/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials")),
    ("ssh out", False, inside("/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "gpu-box", "true")),
    ("internet", False, inside("/usr/bin/curl", "-sm", "5", "https://example.com")),
    ("symlink out, then read", False, inside("/bin/sh", "-c", f"ln -s {outside}/secret.txt l && cat l")),
    ("hardlink out", False, inside("/bin/ln", f"{outside}/secret.txt", f"{ws}/h")),
    ("reach another localhost service", False,
     inside("/usr/bin/curl", "-sm", "5", f"http://127.0.0.1:{other.server_address[1]}/")),
    ("read another app's preferences", False, inside("/usr/bin/defaults", "read", "com.apple.dock")),
    ("read the global preferences", False, inside("/usr/bin/defaults", "read", "-g")),
]
os.link(outside / "secret.txt", ws / "planted")
try:
    cases.append(("launch with a hardlink planted in workspace", False, inside("/bin/cat", "planted") or True))
except PermissionError:
    cases.append(("launch with a hardlink planted in workspace", False, False))

bad = [c for c in cases if c[1] != c[2]]
for name, want, got in cases:
    print(f"{'ok ' if want == got else 'BAD'} {name:42} {'allowed' if got else 'denied'}")
other.shutdown()
os.system(f"rm -rf '{base}'")
print(f"ROOM {len(cases) - len(bad)}/{len(cases)}")
sys.exit(1 if bad else 0)
