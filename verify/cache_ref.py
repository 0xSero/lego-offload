"""Explore cache.bend's world with the real core.salt and compare with the Bend result.
usage: python3 cache_ref.py runs/cache_raw.txt ../results/cache.json
"""
import ast
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

DEPTH, WORLDS = 4, 8
KEY = secrets.token_hex(16)                     # one session's key, as the door draws it
SCOPE = {0: frozenset(), 1: frozenset({"hr"})}  # public job, private job over hr


def salt(cfg, sc):
    return core.salt(KEY, SCOPE[sc]) if cfg == 1 else ""      # mutant: one shared cache


def act(cfg, w, cache, a):
    """One job in world w: (cache after, hit bit). Cache entries are (salt, task, value)."""
    if a < 2:
        return cache | {(salt(cfg, 1), a, w)}, 0
    k = (salt(cfg, 0), (a - 2) // 8, (a - 2) % 8)
    return cache | {k}, int(k in cache)


def explore(cfg):
    traces = bad = 0
    first, most = 99, 1
    stack = [([(frozenset(), 0)] * WORLDS, 0)]
    while stack:
        ws, d = stack.pop()
        if d == DEPTH:
            continue
        for a in range(18):
            nxt = []
            for w, (c, sg) in enumerate(ws):
                c2, h = act(cfg, w, c, a)
                nxt.append((c2, sg * 2 + h))
            k = len({sg for _, sg in nxt})
            traces += 1
            bad += k > 1
            first = min(first, d + 1) if k > 1 else first
            most = max(most, k)
            stack.append((nxt, d + 1))
    return [traces, bad, first, most]


def flat(t):
    out = []
    while isinstance(t, tuple) and len(t) == 2 and isinstance(t[1], tuple):
        out.append(t[0])
        t = t[1]
    out.extend(t if isinstance(t, tuple) else [t])
    return out


assert len({core.salt(KEY, s) for s in (frozenset(), {"hr"}, {"acme"}, {"hr", "acme"})}) == 4, "salt collides"
assert core.salt(KEY, {"hr"}) != core.salt(secrets.token_hex(16), {"hr"}), "salt ignores the session key"
line = next(l for l in open(sys.argv[1]) if l.startswith("Result: "))[8:]
bend = {cfg: flat(r) for cfg, r in ast.literal_eval(line)}
rows, ok = [], True
for cfg, name in ((1, "salted by scope"), (0, "one shared cache")):
    ref = explore(cfg)
    same = bend[cfg] == ref
    ok &= same
    rows.append({"variant": name, "traces": ref[0], "violating": ref[1],
                 "first_violation": "holds" if ref[2] == 99 else f"FAIL@{ref[2]}", "max_classes": ref[3],
                 "bend_matches": same})
ok &= rows[0]["violating"] == 0 and rows[1]["violating"] > 0      # the brick holds, the mutant is caught
json.dump(rows, open(sys.argv[2], "w"), indent=1)
print(f"{'variant':18} {'traces':>8} {'violating':>10} {'noninterference':>16} {'classes':>8}  match")
for x in rows:
    print(f"{x['variant']:18} {x['traces']:>8} {x['violating']:>10} {x['first_violation']:>16} {x['max_classes']:>8}  "
          f"{x['bend_matches']}")
print("CACHE AGREES" if ok else "CACHE MISMATCH")
sys.exit(0 if ok else 1)
