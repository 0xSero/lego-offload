"""Bit-exact Python port of sim.bend; checks Bend's totals and writes the summary table.
usage: python3 sim_ref.py runs/sim_raw.txt runs/sim.json
"""
import ast
import json
import sys

M = 0xFFFFFF


def rng(x):
    return (x * 1664525 + 12345) & M


def pick(r):
    return (r >> 8) % 100


def session(x, budget, t):
    asked = ans = late = over = bits = wait = 0
    left = budget
    for _ in range(40):
        r0 = rng(x); r1 = rng(r0); r2 = rng(r1); r3 = rng(r2)
        x = r3
        kind = pick(r0)
        if kind < 45 or kind >= 80:
            continue
        p = pick(r1)
        b = 2 if p < 30 else 3 if p < 60 else 7 if p < 85 else 10
        asked += 1
        if b > left:
            over += 1
            continue
        left -= b
        bits += b
        jt = 2 + (r2 >> 8) % 30 + (60 if pick(r3) < 10 else 0)
        if jt > t:
            late += 1
        else:
            ans += 1
            wait += t - jt
    return asked, ans, late, over, bits, wait


def cell(budget, t):
    tot = [0] * 6
    for i in range(8192):
        for k, v in enumerate(session(rng((4242 + i * 7919) & M), budget, t)):
            tot[k] += v
    return tot


def flat(v):
    out = []
    while isinstance(v, tuple):
        out.append(v[0])
        v = v[1]
    return out + [v]


line = next(l for l in open(sys.argv[1]) if l.startswith("Result: "))[8:]
bend = [flat(c) for group in flat(ast.literal_eval(line)) for c in group]
rows, ok = [], True
for b in bend:
    budget, t, *vals = b
    ref = cell(budget, t)
    same = vals == ref and max(ref) < 2**24
    ok &= same
    asked, ans, late, over, bits, wait = ref
    rows.append({"budget": budget, "T": t, "asked": asked, "answered": ans, "late": late, "over_budget": over,
                 "bits_per_session": bits / 8192, "answered_pct": 100 * ans / asked,
                 "mean_extra_wait_s": wait / ans if ans else None, "bend_matches": same})
json.dump(rows, open(sys.argv[2], "w"), indent=1)
print(f"{'budget':>6} {'T':>4} {'answered':>9} {'late':>6} {'over':>6} {'bits/sess':>9} {'extra wait':>10}  match")
for r in rows:
    w = "-" if r["mean_extra_wait_s"] is None else f"{r['mean_extra_wait_s']:.1f}s"
    print(f"{r['budget']:>6} {r['T']:>4} {r['answered_pct']:>8.1f}% {100*r['late']/r['asked']:>5.1f}% "
          f"{100*r['over_budget']/r['asked']:>5.1f}% {r['bits_per_session']:>9.1f} {w:>10}  {r['bend_matches']}")
print("SIM MATCHES" if ok else "SIM MISMATCH")
sys.exit(0 if ok else 1)
