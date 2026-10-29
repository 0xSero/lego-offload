"""Compare model.bend with the core-driven reference (model_ref.py) and summarise.

usage: python3 check.py runs/bend_raw.txt runs/ref.json runs/check.json
Asserts equal state counts, diameters and witness depths for every run (7 mutants on the
standard topology, 9 allowance settings, all 512 topologies, 6 mutants x 512 topologies),
and replays each Bend witness trace through core.py to confirm it is real.
"""
import ast
import json
import sys

import model_ref as M


def flat(t):
    out = []
    while isinstance(t, tuple) and len(t) == 2 and isinstance(t[1], tuple):
        out.append(t[0])
        t = t[1]
    out.extend(t if isinstance(t, tuple) else [t])
    return out


def replay(cfg, t, al, trace):
    s = M.pack(None, None, al & 3, al >> 2 & 3, 0, 0)
    for a in trace[:-1]:
        s, ev = M.step(cfg, t, al, s, a)
        if s is None:
            return 0
    return M.step(cfg, t, al, s, trace[-1])[1]


line = next(l for l in open(sys.argv[1]) if l.startswith("Result: "))[8:]
line = line.replace("std_t", str(M.STD_T)).replace("std_al", str(M.STD_AL))
mut, alw, topo, catches = flat(ast.literal_eval(line))
ref = json.load(open(sys.argv[2]))
bad = []

for b, r in zip(mut, ref["mutants"]):
    cfg, t, al, n, diam, ws = flat(b)
    if (n, diam, [w[0] for w in ws]) != (r["states"], r["diameter"], r["depths"]):
        bad.append(("mutant", cfg))
    for e, (d, tr) in enumerate(ws):
        if d != 99 and not replay(cfg, t, al, tr) >> e & 1:
            bad.append(("replay", cfg, M.EVENTS[e]))
for group, name in [(alw, "allowances"), (topo, "topologies")]:
    for b, r in zip(group, ref[name]):
        cfg, t, al, n, diam, ds = flat(b)
        if (cfg, t, al, n, diam, ds) != (r["cfg"], r["t"], r["al"], r["states"], r["diameter"], r["depths"]):
            bad.append((name, cfg, t, al))
ref_catch = {}
for r in ref["catches"]:
    ref_catch[r["cfg"]] = ref_catch.get(r["cfg"], 0) + any(d != 99 for d in r["depths"][:3])
for cfg, k in [flat(c) for c in catches]:
    if ref_catch.get(cfg) != k:
        bad.append(("catch", cfg, k, ref_catch.get(cfg)))

topo_rows = [flat(b) for b in topo]
summary = {
    "mutants": [{"variant": name, "states": flat(b)[3], "diameter": flat(b)[4],
                 **{e: flat(b)[5][i][0] for i, e in enumerate(M.EVENTS)},
                 "witnesses": {e: [M.show(a) for a in flat(b)[5][i][1]] for i, e in enumerate(M.EVENTS)
                               if flat(b)[5][i][0] != 99}}
                for (name, _), b in zip(M.VARIANTS, mut)],
    "allowances": [{"a_bits": flat(b)[2] & 3, "b_bits": flat(b)[2] >> 2, "states": flat(b)[3],
                    "useful_depth": flat(b)[5][3]} for b in alw],
    "topologies": {"count": len(topo_rows), "violations": sum(any(d != 99 for d in r[5][:3]) for r in topo_rows),
                   "states_total": sum(r[3] for r in topo_rows), "max_states": max(r[3] for r in topo_rows),
                   "max_diameter": max(r[4] for r in topo_rows),
                   "useful": sum(r[5][3] != 99 for r in topo_rows)},
    "mutant_catch_over_512_topologies": {name: dict(map(flat, catches)).get(cfg) for name, cfg in M.VARIANTS[1:]},
    "bend_agrees_with_core": not bad, "disagreements": bad[:20],
}
json.dump(summary, open(sys.argv[3], "w"), indent=1)
print(json.dumps({k: v for k, v in summary.items() if k != "mutants"}, indent=1))
for m in summary["mutants"]:
    print(f"{m['variant']:18} states={m['states']:5} " + " ".join(
        f"{e}={'holds' if m[e] == 99 else 'FAIL@' + str(m[e])}" for e in M.EVENTS[:3]))
print("MODEL CHECK AGREES" if not bad else f"MODEL CHECK: {len(bad)} DISAGREEMENTS")
sys.exit(1 if bad else 0)
