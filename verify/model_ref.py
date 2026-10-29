"""Reference model checker that drives the real core.py bricks (the same world as model.bend).

Breadth-first search to a fixpoint. Every decision (labels, moves, runs, the reply row,
shapes, charges) is made by core.py; this file only packs states and records what the
cloud could observe. check.py asserts it agrees with Bend, an independent implementation.
"""
import json
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

LABEL, MOVE, RUN, SHAPE, BUDGET, CLOCK = 1, 2, 4, 8, 16, 32
VARIANTS = [("all bricks", 63), ("label fails open", 62), ("no move check", 61), ("run drops labels", 59),
            ("no shape", 55), ("no budget", 47), ("no clock", 31)]
EVENTS = ["contain", "noninterference", "budget", "useful"]
STD_T = 1 | 2 << 3 | 7 << 6        # pop {a}, ally {b}, mac {a, b, sealed}
STD_AL = 2 | 1 << 2                # 2 bits for a, 1 bit for b
PUBLIC = {"/w/README.md"}
SCOPES = {"/w/hr": "a", "/w/acme": "b"}
SOURCES = ["/w/README.md", "/w/hr/salaries.csv", "/w/acme/contract.md", "/w/.env"]
TRUTH = [frozenset(), frozenset("a"), frozenset("b"), frozenset({core.SEALED})]
SCHEMAS = [None, {"int": [0, 0]}, {"bool": True}]
BIT = {"a": 1, "b": 2, core.SEALED: 4}
N_ACTIONS = 567


def mask(scopes):
    return sum(BIT[s] for s in scopes)


def unmask(m):
    return frozenset(s for s, b in BIT.items() if m & b)


def nodes(t):
    return [core.Node(f"n{i}", unmask(t >> 3 * i & 7)) for i in range(3)]


def home(t, scope):
    return next((i for i, n in enumerate(nodes(t)) if scope <= n.trust), 3)


def pack(r0, r1, la, lb, ka, kb):
    slot = lambda r: 3 if r is None else r[0] | mask(r[1]) << 2 | mask(r[2]) << 5
    return slot(r0) | slot(r1) << 8 | la << 16 | lb << 18 | ka << 20 | kb << 22


def unpack(s):
    slot = lambda v: None if v & 3 == 3 else (v & 3, unmask(v >> 2 & 7), unmask(v >> 5 & 7))
    return slot(s & 255), slot(s >> 8 & 255), s >> 16 & 3, s >> 18 & 3, s >> 20 & 3, s >> 22 & 3


def decode(a):
    return [i for i in range(6) if (a // 9 + 1) >> i & 1], a // 3 % 3, a % 3


def step(cfg, t, al, s, a):
    """Returns (next_state or None if refused, event bits)."""
    r0, r1, la, lb, ka, kb = unpack(s)
    picks, n, ret = decode(a)
    ns = nodes(t)
    node = ns[n]
    contain, moved = 0, []
    for i in picks:
        if i < 4:
            lab = core.label(SOURCES[i], PUBLIC, SCOPES)
            if not cfg & LABEL and lab == {core.SEALED}:
                lab = frozenset()                         # mutant: unlisted means public
            at = 0 if i == 0 else home(t, TRUTH[i])
            item, taint = core.Item(f"s{i}", lab, f"n{at}"), TRUTH[i]
        else:
            r = (r0, r1)[i - 4]
            at = 3 if r is None else r[0]
            item, taint = (None, None) if r is None else (core.Item(f"r{i - 4}", r[1], f"n{r[0]}"), r[2])
        if at == 3:
            return None, 0
        if item.node != node.name:
            if cfg & MOVE:
                try:
                    core.move(item, node)
                except core.Refused:
                    return None, 0
            contain |= not taint <= node.trust
            item = core.Item(item.ref, item.scopes, node.name)
        moved.append((item, taint))
    taint = frozenset().union(*(x for _, x in moved))
    if cfg & RUN:
        try:
            out = core.run([m for m, _ in moved], node, "out")
        except core.Refused:
            return (s, 1) if contain else (None, 0)       # the copy already happened
    else:
        out = core.Item("out", frozenset(), node.name)    # mutant: the output forgets its inputs
    result = (n, out.scopes, taint)
    r0, r1 = (result, r1) if r0 is None else (r0, result)
    schema = SCHEMAS[ret]
    if cfg & BUDGET:
        row, led = core.release_plan(out, schema, {"a": la, "b": lb})
        la, lb = led["a"], led["b"]
    else:
        row = "full" if not out.scopes else ("receipt" if schema is None else "shaped")
    ev = contain
    if row == "full" and taint:
        ev |= 2
    if row == "shaped":
        b = core.bits(schema)
        ka = min(ka + b * ("a" in taint), 3)
        kb = min(kb + b * ("b" in taint), 3)
        if core.SEALED in taint or ka > al & 3 or kb > al >> 2 & 3:
            ev |= 4
        if taint and not (cfg & SHAPE and cfg & CLOCK):
            ev |= 2                                       # raw value, or a data-dependent reply time
        if taint & {"a", "b"}:
            ev |= 8
    return pack(r0, r1, la, lb, ka, kb), ev


def explore(cfg, t, al):
    s0 = pack(None, None, al & 3, al >> 2 & 3, 0, 0)
    seen = {s0: (None, None, 0)}
    best = [None] * 4                                     # (depth, parent, action)
    frontier, d = [s0], 0
    while frontier:
        nxt = []
        for s in frontier:
            for a in range(N_ACTIONS):
                n, ev = step(cfg, t, al, s, a)
                for e in range(4):
                    hit = ev >> e & 1 and (e < 3 or not ev & 7)
                    if hit and best[e] is None:
                        best[e] = (d + 1, s, a)
                if ev & 7 or n is None or n == s or n in seen:
                    continue
                seen[n] = (s, a, d + 1)
                nxt.append(n)
        frontier, d = nxt, d + 1 if nxt else d
    return s0, seen, d, best


def trace(seen, s, a):
    out = [a]
    while seen[s][0] is not None:
        out.append(seen[s][1])
        s = seen[s][0]
    return out[::-1]


def check(args):
    cfg, t, al = args
    s0, seen, d, best = explore(cfg, t, al)
    return {"cfg": cfg, "t": t, "al": al, "states": len(seen), "diameter": d,
            "depths": [99 if b is None else b[0] for b in best],
            "traces": [None if b is None else trace(seen, b[1], b[2]) for b in best]}


def show(a):
    picks, n, ret = decode(a)
    names = ["README", "hr", "acme", ".env", "r0", "r1"]
    return f"offload({'+'.join(names[i] for i in picks)} -> {['pop', 'ally', 'mac'][n]}, {['no shape', '1-bit', 'bool'][ret]})"


def runs():
    mut = [(c, STD_T, STD_AL) for _, c in VARIANTS]
    alw = [(63, STD_T, al) for al in range(16) if al & 3 != 3 and al >> 2 != 3]
    topo = [(63, t, STD_AL) for t in range(512)]
    catch = [(c, t, STD_AL) for _, c in VARIANTS[1:] for t in range(512)]
    return mut, alw, topo, catch


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/ref.json")
    mut, alw, topo, catch = runs()
    with Pool() as p:
        res = {k: p.map(check, v, chunksize=4) for k, v in
               [("mutants", mut), ("allowances", alw), ("topologies", topo), ("catches", catch)]}
    json.dump(res, out.open("w"))
    print(f"wrote {out}: " + ", ".join(f"{k} {len(v)}" for k, v in res.items()))
