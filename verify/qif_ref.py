"""Reference for qif.bend that drives the real core.py bricks: 16 concrete secret worlds,
every action sequence up to a depth, and the Koepf-Basin class bounds per scope.
usage: python3 qif_ref.py DEPTH OUT.json
"""
import json
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from model_ref import BIT, LABEL, MOVE, PUBLIC, RUN, SCOPES, SHAPE, BUDGET, CLOCK, SOURCES, TRUTH, mask, unmask  # noqa: E402

VARIANTS = [63, 62, 61, 59, 55, 47, 31]
NODES = [core.Node("pop", frozenset("a")), core.Node("ally", frozenset("b")),
         core.Node("mac", frozenset({"a", "b", core.SEALED}))]
HOME = [0, 0, 1, 2]
PAIRS = [1, 2, 4, 8, 16, 32, 3, 5, 9, 17, 33, 6, 10, 18, 34, 12, 20, 36, 24, 40, 48]
SCHEMAS = [None, {"int": [0, 0]}, {"bool": True}]
ALLOW = {"a": 2, "b": 1}


def pstep(cfg, pub, a):
    """Public step: same decision in every world. Returns (pub', row, slot) or None."""
    r0, r1, led, charged = pub
    m, n, ret = PAIRS[a // 9], a // 3 % 3, a % 3
    node, items, taint = NODES[n], [], frozenset()
    for i in range(6):
        if not m >> i & 1:
            continue
        if i < 4:
            lab = core.label(SOURCES[i], PUBLIC, SCOPES)
            if not cfg & LABEL and lab == {core.SEALED}:
                lab = frozenset()
            item, x = core.Item(f"s{i}", lab, NODES[HOME[i]].name), TRUTH[i]
        else:
            r = (r0, r1)[i - 4]
            if r is None:
                return None
            item, x = core.Item(f"r{i}", r[1], NODES[r[0]].name), r[2]
        if item.node != node.name:
            if cfg & MOVE:
                try:
                    core.move(item, node)
                except core.Refused:
                    return None
            item = core.Item(item.ref, item.scopes, node.name)
        items.append(item)
        taint |= x
    if cfg & RUN:
        try:
            out = core.run(items, node, "out")
        except core.Refused:
            return None
    else:
        out = core.Item("out", frozenset(), node.name)
    slot = 0 if r0 is None else 1
    res = (n, out.scopes, taint)
    r0, r1 = (res, r1) if slot == 0 else (r0, res)
    schema = SCHEMAS[ret]
    if cfg & BUDGET:
        row, new = core.release_plan(out, schema, dict(led))
        if row == "shaped":
            b = core.bits(schema)
            charged = {s: charged[s] + (b if s in out.scopes else 0) for s in charged}
        led = new
    else:
        row = "full" if not out.scopes else ("receipt" if schema is None else "shaped")
    return (r0, r1, led, charged), (row, ret), slot


def world_value(w, i, vals):
    return [0, w & 3, w >> 2 & 1, w >> 3 & 1, vals[0], vals[1]][i]


def observe(cfg, row, ret, v):
    if row == "full":
        return 1 + v
    if row == "receipt":
        return 5
    if not cfg & CLOCK:
        return 11 + v                              # the reply time depends on the job
    if not cfg & SHAPE:
        return 1 + v
    answer = v if ret == 1 else ([False, True][v] if v < 2 else v)
    reply = core.shape(answer, SCHEMAS[ret])
    if ret == 1:
        return 6 + (reply != 0)
    return 8 + (2 if reply == core.UNKNOWN else int(reply))


def classes(hs, ws):
    return len({hs[w] for w in ws})


def judge(charged, hs):
    ca, cb = charged["a"], charged["b"]
    na = max(classes(hs, [b + j for j in range(4)]) for b in (0, 4, 8, 12))
    nb = max(classes(hs, [b, b + 4]) for b in (0, 1, 2, 3, 8, 9, 10, 11))
    ns = max(classes(hs, [b, b + 8]) for b in range(8))
    nt = classes(hs, range(16))
    bad = (na > 1 << ca) | 2 * (nb > 1 << cb) | 4 * (ns > 1) | 8 * (nt > 1 << ca + cb)
    return bad, na


def walk(cfg, pub, worlds, d, left):
    """worlds: list of (v0, v1, history). Returns [nodes, fa, fb, fs, ft, max a-classes]."""
    acc = [1, 99, 99, 99, 99, 0]
    if left == 0:
        return acc
    for a in range(189):
        r = pstep(cfg, pub, a)
        if r is None:
            continue
        pub1, (row, ret), slot = r
        m = PAIRS[a // 9]
        ws1 = []
        for w, (v0, v1, h) in enumerate(worlds):
            v = sum(world_value(w, i, (v0, v1)) for i in range(6) if m >> i & 1) % 4
            o = observe(cfg, row, ret, v)
            ws1.append((v, v1, h * 16 + o) if slot == 0 else (v0, v, h * 16 + o))
        bad, na = judge(pub1[3], [h for _, _, h in ws1])
        acc[5] = max(acc[5], na)
        if bad:
            acc[0] += 1
            for k in range(4):
                if bad >> k & 1:
                    acc[1 + k] = min(acc[1 + k], d + 1)
            continue
        sub = walk(cfg, pub1, ws1, d + 1, left - 1)
        acc[0] += sub[0]
        acc[1:5] = [min(x, y) for x, y in zip(acc[1:5], sub[1:5])]
        acc[5] = max(acc[5], sub[5])
    return acc


def run(args):
    cfg, depth = args
    pub = (None, None, {"a": ALLOW["a"], "b": ALLOW["b"]}, {"a": 0, "b": 0})
    return {"cfg": cfg, "depth": depth, "result": walk(cfg, pub, [(0, 0, 0)] * 16, 0, depth)}


if __name__ == "__main__":
    depth = int(sys.argv[1])
    with Pool() as p:
        res = p.map(run, [(c, depth) for c in VARIANTS])
    json.dump(res, open(sys.argv[2], "w"), indent=1)
    for r in res:
        print(r["cfg"], r["result"])
