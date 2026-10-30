"""Every agent adapter, without a model: what the agent VM would hold.

For each installed agent: build its throwaway HOME exactly as `lego AGENT` does, then check
(1) no real credential appears in any file, the argv or the environment (values are compared
in memory, never printed), (2) its model traffic is pointed at the route and its tools at the
door, and nothing else.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
import route  # noqa: E402

ROUTE, DOOR = "http://127.0.0.1:1111", "http://127.0.0.1:2222/mcp"


def secrets_on_host():
    """Every credential the route may inject, plus the parts that make one valid."""
    out = []
    for name, (_, _, auth, _) in route.UPSTREAMS.items():
        try:
            out += [v.split(" ", 1)[-1] for v in auth().values()]
        except Exception:                                   # that agent is not logged in here
            continue
    return [s for s in out if len(s) >= 16]


def main():
    found, failed = secrets_on_host(), 0
    print(f"{len(found)} host credentials to look for")
    for name, (adapter, upstream) in agents.AGENTS.items():
        home = tempfile.mkdtemp(prefix="lego-test-home-")
        try:
            try:
                argv, env = adapter(home, ROUTE, DOOR, ["-p", "hi"], home, **(
                    {"model": {"model": "local-model", "url": "http://127.0.0.1:9/v1"}} if upstream is None else {}))
            except SystemExit as e:
                print(f"skip {name:7} {e}")
                continue
            blob = "\n".join([*argv, *env.values()] + [p.read_text(errors="replace")
                                                        for p in Path(home).rglob("*") if p.is_file()])
            leaked = sum(s in blob for s in found)
            routed = ROUTE in blob
            tools = DOOR in blob
            other = [u for u in ("api.anthropic.com",) if u in blob]
            ok = not leaked and routed and tools and not other
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name:7} credentials in VM: {leaked}, model via route: {routed}, "
                  f"tools via door: {tools}, upstream hosts named: {other or 'none'}, upstream {upstream or 'local model'}")
        finally:
            shutil.rmtree(home, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
