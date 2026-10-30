"""Contention probe: can a public job's reply time reveal private data through a job running
beside it on the same model node?

    python3 tests/contention.py results/contention.json [TRIALS]

Each trial starts a free private job (no `returns`, so a receipt) whose length depends on a
secret: a long essay if one person's salary is over 150000, one word otherwise. It then asks
for a public job on the same node and records the reply row and time. Half the trials ask
about a person above 150000, half about one below. If public reply times differ between the
two halves, the door leaks one uncharged bit per pair of calls. Synthetic data on gpu-box,
deleted afterwards.
"""
import json
import random
import secrets
import signal
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import door  # noqa: E402
import e2e  # noqa: E402

GAP = 3
PRIVATE = ("Look up {name} in the CSV. If their salary is above 150000, write a 2500-word essay about "
           "salary bands. Otherwise reply with the single word OK.")
PUBLIC = "Write the integers from 1 to 800, separated by single spaces, and nothing else."


def main(out, trials):
    env = e2e.setup(random.Random(secrets.randbits(32)))
    rows = []
    try:
        p, state = env["policy"], env["base"] / ".lego"
        d = door.Door(p, state)
        hr, readme = p["catalog"][0], "README.md"
        hi = [n for n, s in env["people"] if s > 150000][0]
        lo = [n for n, s in env["people"] if s <= 150000][0]
        plan = [("above", hi), ("below", lo)] * trials
        random.Random(1).shuffle(plan)
        for side, name in plan:
            d.reset()
            t0 = time.time()
            first = d.offload(PRIVATE.format(name=name), [hr])
            time.sleep(GAP)                                     # let the private job start generating
            t1 = time.time()
            r = d.offload(PUBLIC, [readme])
            took = time.time() - t1
            e2e.finish_jobs(p["job_timeout"])                   # let the private job end before the next trial
            log = [json.loads(l) for l in (state / "door.jsonl").read_text().splitlines()]
            ran = next((e["took"] for e in log if e["op"] == "done" and e["ref"] == first["ref"]), None)
            rows.append({"salary": side, "private_row": first["row"], "private_seconds": ran,
                         "public_row": r["row"], "public_seconds": round(took, 2)})
            print(f"{side:5}  private {first['row']:8} ran {ran}s  public {r['row']:8} {took:6.2f}s", flush=True)
        e2e.finish_jobs(p["job_timeout"])
    finally:
        e2e.transport._sh(e2e.POP, f"rm -rf {e2e.shlex.quote(env['pop'])}")
        e2e.subprocess.run(["rm", "-rf", str(env["base"])], timeout=60)
    full = {s: [r["public_seconds"] for r in rows if r["salary"] == s and r["public_row"] == "full"]
            for s in ("above", "below")}
    ran = {s: [r["private_seconds"] for r in rows if r["salary"] == s] for s in ("above", "below")}
    summary = {"date": time.strftime("%Y-%m-%d %H:%M"), "trials_per_side": trials, "rows": rows,
               "full_replies": {s: len(v) for s, v in full.items()}, "private_seconds": ran,
               "mean_full_seconds": {s: round(statistics.mean(v), 2) if v else None for s, v in full.items()},
               "separable": bool(full["above"] and full["below"] and min(full["above"]) > max(full["below"]))}
    Path(out).write_text(json.dumps(summary, indent=1))
    print(f"CONTENTION full replies {summary['full_replies']}, mean seconds {summary['mean_full_seconds']}, "
          f"above and below separable by time: {summary['separable']}")


if __name__ == "__main__":
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    main(sys.argv[1] if len(sys.argv) > 1 else "results/contention.json", int(sys.argv[2]) if len(sys.argv) > 2 else 3)
