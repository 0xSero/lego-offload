"""Each brick tested alone, exhaustively over a small domain. Run: python3 test_core.py"""
import itertools
import sys
from pathlib import Path
import random

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from core import SEALED, UNKNOWN, Item, Node, Refused  # noqa: E402

PUBLIC = {"/w/README.md", "/w/src/app.py"}
SCOPES = {"/w/hr": "a", "/w/acme": "b", "/w/acme/hr": "a"}
SCOPE_SETS = [frozenset(c) for n in range(4) for c in itertools.combinations(["a", "b", SEALED], n)]


def refused(f, *a):
    try:
        f(*a)
    except Refused:
        return True
    return False


def test_label():
    cases = {
        "/w/README.md": set(), "/w/src/app.py": set(),                       # listed public
        "/w/hr/x.csv": {"a"}, "/w/hr": {"a"}, "/w/acme/c.md": {"b"},         # scopes
        "/w/acme/hr/p.csv": {"a", "b"},                                      # nested scopes join
        "/w/hrx/x.csv": {SEALED}, "/w/notes.txt": {SEALED}, "/w/.env": {SEALED},  # unlisted
        "w/README.md": {SEALED}, "/w/./README.md": {SEALED}, "/w/src/../README.md": {SEALED},
        "/w//README.md": {SEALED}, "/w/README.md/": {SEALED}, "/W/README.md": {SEALED},
        "/w/café.md": {SEALED},                                        # not NFC
        "": {SEALED},
    }
    for p, want in cases.items():
        assert core.label(p, PUBLIC, SCOPES) == want, (p, core.label(p, PUBLIC, SCOPES))
    assert refused(core.check_policy, {"/w/hr/ok.md"}, SCOPES)               # public inside a scope
    assert refused(core.check_policy, {"rel/path"}, SCOPES)
    core.check_policy(PUBLIC, SCOPES)


def test_move_and_run():
    nodes = [Node(f"n{i}", t) for i, t in enumerate(SCOPE_SETS)]
    for node, sc in itertools.product(nodes, SCOPE_SETS):
        item = Item("x", sc, "elsewhere")
        assert refused(core.move, item, node) == (not sc <= node.trust)
    for node, s1, s2 in itertools.product(nodes, SCOPE_SETS, SCOPE_SETS):
        ins = [Item("i", s1, node.name), Item("j", s2, node.name)]
        if (s1 | s2) <= node.trust:
            assert core.run(ins, node, "o").scopes == s1 | s2                 # labels never shrink
        else:
            assert refused(core.run, ins, node, "o")
    assert refused(core.run, [Item("i", frozenset(), "other")], nodes[0], "o")  # not moved yet


def test_shape():
    assert core.bits({"bool": True}) == 2                                     # UNKNOWN is an outcome
    assert core.bits({"int": [0, 0]}) == 1
    assert core.bits({"enum": ["live", "test"]}) == 2
    assert core.bits({"int": [0, 365]}) == 9
    assert core.bits({"int": [0, 65534]}) == 16
    assert refused(core.bits, {"int": [0, 65535]})                            # 17 bits
    assert refused(core.bits, {"tuple": [{"int": [0, 999]}] * 3})             # packing facts
    for bad in [{}, {"string": True}, {"enum": []}, {"enum": ["a", "a"]}, {"int": [5, 1]},
                {"int": [0, 1.5]}, {"enum": [[1]]}, {"tuple": []},
                {"tuple": ["int", "int"]}, "int", None, 5, [], {"bool": False}, {"bool": 1},
                {"bool": True, "int": [0, 1]}, {"int": [0]}, {"int": [0, 1, 2]}, {"int": "0-5"},
                {"int": [True, 3]}, {"enum": "abc"}, {"tuple": "ab"}, {"tuple": {"int": [0, 1]}}]:
        assert refused(core.size, bad), bad
    deep = {"bool": True}
    for _ in range(50):
        deep = {"tuple": [deep]}
    assert refused(core.size, deep)                                           # nesting is bounded
    rnd = random.Random(11)                                                   # any JSON: an int or a refusal
    atoms = [None, True, False, 0, 1, -3, 2.5, "", "int", "bool", "enum", "tuple", [], {}]

    def junk(d):
        r = rnd.random()
        if d > 3 or r < 0.3:
            return rnd.choice(atoms)
        if r < 0.6:
            return [junk(d + 1) for _ in range(rnd.randrange(4))]
        return {rnd.choice(["bool", "enum", "int", "tuple", "x"]): junk(d + 1) for _ in range(rnd.randrange(1, 3))}
    for _ in range(20000):
        try:
            assert type(core.size(junk(0))) is int
        except Refused:
            pass
    rnd = random.Random(7)
    schemas = [{"bool": True}, {"enum": ["live", "test", 3]}, {"int": [0, 365]},
               {"tuple": [{"bool": True}, {"int": [1, 3]}]}]
    pool = [True, False, 0, 1, 3, 365, 366, -1, 1.0, "live", "Live", "test", None, [], [True, 2],
            [True, 4], [1, 2], "x" * 10**5, {"a": 1}, 2**64]
    for _ in range(10000):
        sc, v = rnd.choice(schemas), rnd.choice(pool)
        out = core.shape(v, sc)
        assert out == UNKNOWN or (core.member(out, sc) and out is v)
    assert core.shape(1, {"bool": True}) == UNKNOWN and core.shape(True, {"int": [0, 1]}) == UNKNOWN


def test_charge():
    for la, b in itertools.product(range(4), range(1, 4)):
        for sc in SCOPE_SETS:
            if not sc:
                continue
            ledger = {"a": la, "b": 1}
            ok = SEALED not in sc and all(ledger[s] >= b for s in sc)
            if ok:
                new = core.charge(ledger, sc, b)
                assert all(new[s] == ledger[s] - b >= 0 for s in sc)
                assert all(new[s] == ledger[s] for s in ledger if s not in sc)
            else:
                assert refused(core.charge, ledger, sc, b)


def test_clock_and_release():
    assert core.clock(5, 10, 10) == 5 and core.clock(5, 11, 10) == UNKNOWN
    pub, a, sealed = Item("p", frozenset(), "m"), Item("x", frozenset("a"), "m"), Item("s", frozenset({SEALED}), "m")
    led = {"a": 2}
    assert core.release_plan(pub, None, led) == ("full", led)
    assert core.release_plan(a, None, led) == ("receipt", led)
    assert core.release_plan(a, {"bool": True}, led) == ("shaped", {"a": 0})
    assert core.release_plan(a, {"int": [0, 7]}, led) == ("receipt", led)    # 4 bits > 2 left
    assert core.release_plan(sealed, {"int": [0, 0]}, {"a": 9, SEALED: 9}) == ("receipt", {"a": 9, SEALED: 9})
    # the reply never depends on the value except through the declared shape
    for v, took in itertools.product([True, "leak", 10**9], [0, 9, 99]):
        assert core.deliver("receipt", v, None, took, 30) == (0, core.RECEIPT)
        t, reply = core.deliver("shaped", v, {"bool": True}, took, 30)
        assert t == 30 and reply in (True, UNKNOWN)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
