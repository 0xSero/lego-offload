"""Lego core: three types and seven bricks for moving data and work between machines.

Pure: no I/O, no clock, no network. Adapters (door, transport, room) do the I/O and ask these functions
what is allowed. verify/Lego.lean proves the same rules, and verify/twin.py checks it against these functions.

Types:  Scope = str      a name such as "hr"; an empty scope set means public
        Node(name, trust)          a machine and the scopes it may hold
        Item(ref, scopes, node)    some data, its scopes, the machine holding the bytes
Bricks: label  move  run  shape  charge  clock  queue   (release composes shape, charge, clock)
        scrubbed: the scopes of an item's scrubbed form ("hr" -> "hr~"), a scope of its own
"""
import hashlib
import math
import posixpath
import unicodedata
from dataclasses import dataclass

SEALED = "sealed"          # scope of every path nobody listed; no allowance, ever
UNKNOWN = "UNKNOWN"        # the one value a shaped reply takes when anything goes wrong
KINDS = ("bool", "enum", "int", "tuple")
MAX_BITS = 16              # widest single answer (red team: one 63-bit int carried 3 facts)


class Refused(Exception):
    """A brick said no. Messages depend only on labels and policy, never on content."""


@dataclass(frozen=True)
class Node:
    name: str
    trust: frozenset


@dataclass(frozen=True)
class Item:
    ref: str
    scopes: frozenset
    node: str


# ---------- 1. label: path -> scopes (fail-closed) ----------

def normal(ref):
    """An absolute, normalized, NFC path, optionally prefixed by its node: `pop:/data/x`."""
    path = ref if ref.startswith("/") else ref.partition(":")[2]
    return (path.startswith("/") and posixpath.normpath(path) == path
            and unicodedata.normalize("NFC", ref) == ref)


def label(path, public, scopes):
    """`public`: exact paths; `scopes`: {directory: scope}. Unlisted or odd paths are sealed.
    Paths may carry a node prefix (`pop:/data/hr`); prefixes then match as part of the path."""
    if not normal(path):
        return frozenset({SEALED})
    hits = frozenset(s for d, s in scopes.items() if path == d or path.startswith(d.rstrip("/") + "/"))
    return hits or (frozenset() if path in public else frozenset({SEALED}))


def check_policy(public, scopes):
    """A public path inside a scope is a contradiction: refuse to load rather than guess."""
    for p in public:
        if label(p, {p}, scopes):
            raise Refused(f"public path {p} is inside a scope or is not a normal absolute path")


# ---------- 2. move: the only way bytes cross machines ----------

def may_hold(node, scopes):
    return scopes <= node.trust


def scrubbed(scopes):
    """Scrubbing (scrub.py) keeps the data and swaps its identifiers. The result is labelled with the
    scope's scrubbed twin, "hr~", which a policy grants on its own. Sealed data is never scrubbed."""
    return frozenset(s if s == SEALED or s.endswith("~") else s + "~" for s in scopes)


def move(item, node):
    if not may_hold(node, item.scopes):
        raise Refused(f"{node.name} is not trusted with {sorted(item.scopes)}")
    return Item(item.ref, item.scopes, node.name)


# ---------- 3. run: work happens where its inputs are; outputs keep every input scope ----------

def run(inputs, node, ref):
    if any(i.node != node.name for i in inputs):
        raise Refused("move the inputs to the node first")
    return move(Item(ref, frozenset().union(*(i.scopes for i in inputs)), node.name), node)


def salt(key, scopes):
    """A node's prompt cache is state its jobs share: a hit makes a reply faster. Jobs share it
    only with jobs of the same scopes under the same key (one key per session), so no reply
    time depends on another scope's data. Sent to the node as vLLM's cache_salt."""
    return hashlib.sha256(f"{key}:{','.join(sorted(scopes))}".encode()).hexdigest()[:32]


# ---------- 4. shape: a finite answer set declared before the job runs ----------
# schema: {"bool": True} | {"enum": [..]} | {"int": [lo, hi]} | {"tuple": [schema, ..]}

def size(schema, depth=0):
    """Number of answers a schema allows. Anything that is not exactly one of the four forms is refused."""
    if not isinstance(schema, dict) or len(schema) != 1 or depth > 4 or next(iter(schema)) not in KINDS:
        raise Refused('returns must be one of {"bool": true}, {"enum": ["a", "b"]}, {"int": [0, 10]} or '
                      '{"tuple": [{"bool": true}, {"int": [0, 3]}]}')
    (kind, arg), = schema.items()
    if kind == "bool" and arg is True:
        return 2
    if kind == "enum" and isinstance(arg, list) and arg and all(type(o) in (str, int) for o in arg) \
            and len(set(map(repr, arg))) == len(arg):
        return len(arg)
    if kind == "int" and isinstance(arg, list) and len(arg) == 2 and all(type(x) is int for x in arg) and arg[0] <= arg[1]:
        return arg[1] - arg[0] + 1
    if kind == "tuple" and isinstance(arg, list) and arg:
        return math.prod(size(s, depth + 1) for s in arg)
    raise Refused(f"bad {kind} schema")


def bits(schema):
    """Cost of one answer: UNKNOWN is one more outcome, so a bool costs 2 bits."""
    b = math.ceil(math.log2(size(schema) + 1))
    if b > MAX_BITS:
        raise Refused(f"answer carries {b} bits; the limit is {MAX_BITS}")
    return b


def member(value, schema):
    if schema.get("bool"):
        return type(value) is bool
    if "enum" in schema:
        return any(type(value) is type(o) and value == o for o in schema["enum"])
    if "int" in schema:
        lo, hi = schema["int"]
        return type(value) is int and lo <= value <= hi
    return (type(value) is list and len(value) == len(schema["tuple"])
            and all(member(v, s) for v, s in zip(value, schema["tuple"])))


def shape(value, schema):
    return value if member(value, schema) else UNKNOWN


# ---------- 5. charge: bits per scope, debited before anything private is read ----------

def charge(ledger, scopes, b):
    """Return the new ledger, or refuse. Sealed (and any unfunded scope) has 0 bits."""
    if any(ledger.get(s, 0) < b for s in scopes if s != SEALED) or SEALED in scopes:
        raise Refused("not enough allowance")
    return {**ledger, **{s: ledger[s] - b for s in scopes}}


# ---------- 6. clock: the reply time never depends on the job ----------

def clock(value, took, deadline):
    """The door replies at exactly `deadline`; a job that ran late answers UNKNOWN."""
    return value if took <= deadline else UNKNOWN


# ---------- 7. queue: one job at a time per model node ----------
# A node's slot is (bound, scopes): the latest its last job can end, and that job's scopes.

def queue(slot, t, scopes):
    """A job reaching the door at t: (scopes it also carries, earliest start). A private job starts as soon as the
    node is free, so if the job before it may still run, its timing depends on that job: it carries that job's
    scopes. A public job carries none; it waits for the bound, when the node is surely idle."""
    bound, prior = slot
    if not scopes:
        return frozenset(), max(t, bound)
    return (prior if t < bound else frozenset()), t


def book(slot, t, scopes, timeout):
    """The slot after a private job: it ends at most `timeout` after its latest start."""
    return (max(t, slot[0]) + timeout, scopes)


# ---------- composition: what an offloaded result may send back ----------

def release_plan(item, schema, ledger):
    """Decide the reply row from labels and the ledger only, before any private byte is read.
    Returns (row, ledger): 'full' for public results, 'shaped' after a successful charge,
    'receipt' otherwise (no schema, sealed, or not enough allowance)."""
    if not item.scopes:
        return "full", ledger
    if schema is None:
        return "receipt", ledger
    try:
        return "shaped", charge(ledger, item.scopes, bits(schema))
    except Refused:
        return "receipt", ledger

