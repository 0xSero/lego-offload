"""Twin: the Lean model (Lego.lean, `step real`) and the real door decide the same random calls.

    python3 verify/twin.py [CASES] [SEED]

Each case is a random policy (a host trusted with everything and up to three model nodes with
random trust), a random catalog and a random sequence of calls at random times. For every call
both sides report the row, the output's scopes, the bits charged, the ledger, and the node's
queue slot (bound and scopes). Python runs door.Door.decide, the method offload uses, so this
covers core.queue, core.book, core.release_plan, core.charge, core.bits and pick_node. A
public job's slot after its run is its simulated finish (start + min(duration, timeout)).

The caller may see some scopes (`seen`; a scrubbed twin such as hr~ is just another scope). The door
runs every brick on the scopes hidden from the caller, so Lean gets each item's hidden scopes and the
proof's "public" means "nothing hidden". Model nodes trust everything the caller sees, as a node
that a job may be moved to must.
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import core  # noqa: E402
import door  # noqa: E402
door.measured = lambda node: False                            # stub nodes: no real disk to measure

SCOPES = {1: "hr", 2: "fin", 3: "med"}                         # Lean scope n <-> Python name; 0 is sealed
NAME = {0: core.SEALED, **SCOPES}
NUM = {v: k for k, v in NAME.items()}


def gen(rng):
    seen = sorted(rng.sample([1, 2, 3], rng.choice([0, 0, 0, 1, 1, 2])))
    nodes = [(0, False, [0, 1, 2, 3])] + [(n, True, sorted(set(rng.sample([1, 2, 3], rng.choice([1, 2, 3, 3, 3]))) | set(seen)))
                                         for n in range(1, rng.randint(2, 4))]
    allow = [(s, rng.randint(0, 12)) for s in (1, 2, 3)]
    items = [(0, [])] + [(0, [rng.choice([0, 1, 1, 2, 2, 3])]) for _ in range(rng.randint(1, 4))]
    calls, t = [], 0
    for c in range(rng.randint(1, 14)):
        t += rng.choice([0, 0, 1, 5, 20, 30, 40, 100, 150])           # 30, 100: land on a bound
        k = rng.choice([0, 0, 1, 2, 3, 7, 100, 1 << 16, 1 << 17])
        ins = [rng.randrange(len(items) + c // 2 + 1) for _ in range(rng.choice([0, 1, 1, 1, 2, 3]))]
        calls.append((t, rng.choice([n for n, *_ in nodes] + [1, 1, 9]), k, rng.choice([0, 3, 25, 31, 90, 200]), ins))
    return {"policy": (30, 100, core.MAX_BITS), "nodes": nodes, "allow": allow, "items": items, "calls": calls,
            "seen": seen}


def to_text(case):
    out = ["case", "policy " + " ".join(map(str, case["policy"]))]
    out += [f"node {n} {int(m)} " + " ".join(map(str, ts)) for n, m, ts in case["nodes"]]
    out += [f"allow {s} {b}" for s, b in case["allow"]]
    out += [f"item {n} " + " ".join(str(s) for s in ss if s not in case["seen"]) for n, ss in case["items"]]
    out += [f"call {t} {n} {k} {d} " + " ".join(map(str, ins)) for t, n, k, d, ins in case["calls"]]
    return "\n".join(out + ["end"])


def line(row, scopes, bits, ledger, slot):
    nums = sorted(NUM[s] for s in scopes)
    return (f"{row} [{', '.join(map(str, nums))}] {bits} [{', '.join(str(ledger.get(NAME[s], 0)) for s in range(4))}] "
            f"{int(slot[0])} [{', '.join(map(str, sorted(NUM[s] for s in slot[1])))}]")


def python(case, state):
    deadline, job_timeout, _ = case["policy"]
    policy = {"host": "n0", "workspace": "/ws", "public": ["/pub/0"], "deadline": deadline,
              "job_timeout": job_timeout, "allowance": {SCOPES[s]: b for s, b in case["allow"]},
              "scopes": {f"n0:/data/{v}": v for v in SCOPES.values()},
              "nodes": {**{f"n{n}": {"trust": [NAME[s] for s in ts], "model": m, "out": f"/out{n}"}
                           for n, m, ts in case["nodes"]}, "caller": {"trust": [NAME[s] for s in case["seen"]]}}}
    d = door.Door(policy, state, "caller")
    d.reset()
    refs = []                                                  # Lean's σ.items, in order
    for i, (n, ss) in enumerate(case["items"]):
        path = "/pub/0" if not ss else f"/data/{NAME[ss[0]]}/{i}" if ss[0] else f"/secret/{i}"
        refs.append(f"n{n}:{path}")
    out = []
    for t, n, k, dur, ins in case["calls"]:
        name, returns = f"n{n}", {"int": [0, k - 1]} if k else None
        try:
            if any(i >= len(refs) for i in ins):
                raise core.Refused("no such input")
            items = [d.item(refs[i]) for i in ins]
            d.pick_node(items, name)
            if returns is not None:
                core.bits(returns)
        except core.Refused:
            out.append(line("refused", [], 0, json.loads(d.ledger_file.read_text()), d.slots.get(name, (0, frozenset()))))
            continue
        o, row, led, start, end = d.decide(name, items, returns, t)
        d.refs[o.ref] = o.scopes
        refs.append(o.ref)
        if row == "full":
            d.slots[name] = (min(start + dur, end), frozenset())
        out.append(line(row, d.hidden(o.scopes), core.bits(returns) if row == "shaped" else 0, led, d.slots[name]))
    return out


def main(n_cases=400, seed=0):
    rng = random.Random(seed)
    cases = [gen(rng) for _ in range(n_cases)]
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "cases.txt"
        f.write_text("\n".join(map(to_text, cases)) + "\n")
        r = subprocess.run([str(Path.home() / ".elan/bin/lean"), "--run", str(HERE / "Lego.lean"), str(f)],
                           capture_output=True, text=True, timeout=900, cwd=HERE)
        if r.returncode:
            sys.exit(f"TWIN lean failed: {r.stderr[-2000:] or r.stdout[-2000:]}")
        lean = [b.strip().splitlines() for b in r.stdout.split("end\n")][:len(cases)]
        bad, calls, rows = 0, 0, {}
        for i, case in enumerate(cases):
            py = python(case, Path(tmp) / f"s{i}")
            calls += len(py)
            for l in py:
                rows[l.split()[0]] = rows.get(l.split()[0], 0) + 1
            if py != lean[i]:
                bad += 1
                if bad <= 3:
                    print(f"case {i} differs:\n{to_text(case)}\n  lean:   " + "\n          ".join(lean[i]) +
                          "\n  python: " + "\n          ".join(py))
    print(f"TWIN {'AGREE' if not bad else 'DISAGREE'}: {len(cases) - bad}/{len(cases)} random cases, {calls} calls, "
          f"rows {dict(sorted(rows.items()))}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:3]))
