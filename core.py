"""Lego core: three types and six bricks for moving data and work between machines.

Pure: no I/O, no clock, no network. Adapters (door, transport, room) do the I/O and
ask these functions what is allowed. model.bend checks the same rules exhaustively,
and model_ref.py drives these exact functions to cross-check Bend.

Types:  Scope = str      a name such as "hr"; an empty scope set means public
        Node(name, trust)          a machine and the scopes it may hold
        Item(ref, scopes, node)    some data, its scopes, the machine holding the bytes
Bricks: label  move  run  shape  charge  clock      (release composes the last three)
"""
import math
import posixpath
import unicodedata
from dataclasses import dataclass

SEALED = "sealed"          # scope of every path nobody listed; no allowance, ever
UNKNOWN = "UNKNOWN"        # the one value a shaped reply takes when anything goes wrong
RECEIPT = {"status": "received"}
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
    node, sep, path = ref.partition(":")
    if not sep or node.startswith("/"):
        path = ref
    return (path.startswith("/") and posixpath.normpath(path) == path
            and unicodedata.normalize("NFC", ref) == ref)


def label(path, public, scopes):
    """`public`: exact paths; `scopes`: {directory: scope}. Unlisted or odd paths are sealed.
    Paths may carry a node prefix (`pop:/data/hr`); prefixes then match as part of the path."""
    if not normal(path):
        return frozenset({SEALED})
    hits = frozenset(s for d, s in scopes.items() if path == d or path.startswith(d.rstrip("/") + "/"))
    if hits:
        return hits
    return frozenset() if path in public else frozenset({SEALED})


def check_policy(public, scopes):
    """A public path inside a scope is a contradiction: refuse to load rather than guess."""
    for p in public:
        if label(p, {p}, scopes):
            raise Refused(f"public path {p} is inside a scope or is not a normal absolute path")


# ---------- 2. move: the only way bytes cross machines ----------

def may_hold(node, scopes):
    return scopes <= node.trust


def move(item, node):
    if not may_hold(node, item.scopes):
        raise Refused(f"{node.name} is not trusted with {sorted(item.scopes)}")
    return Item(item.ref, item.scopes, node.name)


# ---------- 3. run: work happens where its inputs are; outputs keep every input scope ----------

def run(inputs, node, ref):
    if any(i.node != node.name for i in inputs):
        raise Refused("move the inputs to the node first")
    scopes = frozenset().union(*(i.scopes for i in inputs))
    if not may_hold(node, scopes):
        raise Refused(f"{node.name} is not trusted with {sorted(scopes)}")
    return Item(ref, scopes, node.name)


# ---------- 4. shape: a finite answer set declared before the job runs ----------
# schema: {"bool": True} | {"enum": [..]} | {"int": [lo, hi]} | {"tuple": [schema, ..]}

def size(schema):
    if schema.get("bool"):
        return 2
    if "enum" in schema:
        opts = schema["enum"]
        if not opts or len(set(map(repr, opts))) != len(opts) or not all(type(o) in (str, int) for o in opts):
            raise Refused("enum needs distinct str/int options")
        return len(opts)
    if "int" in schema:
        lo, hi = schema["int"]
        if type(lo) is not int or type(hi) is not int or hi < lo:
            raise Refused("int needs [lo, hi]")
        return hi - lo + 1
    if "tuple" in schema and schema["tuple"]:
        return math.prod(size(s) for s in schema["tuple"])
    raise Refused("schema must be bool, enum, int or tuple")


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


def deliver(row, value, schema, took, deadline):
    """Returns (seconds_after_submit, reply)."""
    if row == "full":
        return 0, value
    if row == "receipt":
        return 0, RECEIPT
    return deadline, clock(shape(value, schema), took, deadline)
