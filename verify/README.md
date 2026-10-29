# Formal verification

Everything that checks the design lives here. Each check is written twice:

- once in [Bend](https://github.com/HigherOrderCO/Bend), from the spec and independent of `core.py`;
- once in Python, as a twin whose every decision is a call into the real `core.py`.

A checker script then asserts that the two agree on every number. A bug therefore has to land identically in two separate encodings before it can hide, and that is how a Bend compiler bug was found (see the end of this page).

Run it all with `../run verify` and `../run sim`. Results go to `../results/`.

| Layer | Bend | Python twin | Comparator | What it proves |
|-|-|-|-|-|
| 1. Brick laws | `lemmas.bend` | `lemmas_ref.py` | inside `lemmas_ref.py` | Each brick obeys its algebra on a full small domain |
| 2. Model check | `model.bend` | `model_ref.py` | `check.py` | Over every reachable state and every trust topology, no data reaches an untrusted node, and the cloud learns only what it paid for |
| 3. Leak counting | `qif.bend` | `qif_ref.py` | `qif_check.py` | On 16 concrete secret worlds, every attack of up to 3 offloads splits them into at most 2^(bits paid) classes per scope |
| 4. Simulation | `sim.bend` | `sim_ref.py` | inside `sim_ref.py` | How useful the system is under budget and deadline: bit-exact Monte Carlo |

## The thing being checked

`core.py` holds three types and six bricks:

- `label`: a path gets its scopes. Unlisted paths are `sealed`.
- `move`: bytes go only to a node trusted with all of their scopes.
- `run`: the output keeps every input scope.
- `shape`: the answer comes from a finite set declared in advance, or is `UNKNOWN`.
- `charge`: bits are paid per scope before anything is read. Sealed is never chargeable.
- `clock`: the reply always comes at a fixed deadline.

The cloud is a node trusted with nothing. It may call `offload(inputs, node, returns)` as often as it likes.

## 1. Brick laws (`lemmas.bend`)

| Law | Cases | Claim |
|-|-|-|
| L1 lattice | 512 | Scope union is commutative, associative, idempotent, monotone, and an upper bound |
| L2 trust | 4096 | A run accepts inputs x and y exactly when x∪y fits the node. Fitting is monotone in trust |
| L3 shape cost | 65,536 | `bits(n) = ceil(log2(n+1))` is the least b with 2^b ≥ n+1. Sizes over 65,535 are refused. The Bend sum equals the sum over `core.bits` |
| L4 charge | 512 | A balance never goes negative, and a refusal happens exactly when the balance is short |

## 2. Model check (`model.bend`)

This is a breadth-first search to a fixpoint over a world that is small but complete:

- **Nodes:** 3 job nodes with any trust masks, plus the cloud.
- **Scopes:** `a` (hr), `b` (acme) and `sealed`.
- **Data:** 4 sources (README public, hr, acme, and `.env` unlisted) and 2 result slots, so jobs can be chained.
- **Actions:** the cloud picks any non-empty subset of the 6 items, any node, and a return type of none, a 1-bit answer, or a bool. That is 567 actions per state.

Three safety properties are checked on every transition:

- **contain:** no node ever holds bytes whose true scopes it is not trusted with.
- **noninterference:** nothing the cloud sees (bytes or reply time) depends on private data, except through a charged, shaped answer.
- **budget:** the bits learnt about each scope never exceed its allowance, and nothing is ever learnt about `sealed`.

A fourth event, **useful**, is a witness that the cloud can actually get a private-derived answer, so the system cannot pass by refusing everything.

**Mutation testing.** Each brick can be switched off (config bits 1 LABEL, 2 MOVE, 4 RUN, 8 SHAPE, 16 BUDGET, 32 CLOCK). The full system must hold everywhere. Each mutant must be caught, which shows that every brick is load-bearing.

The model runs:

- 7 variants on the standard topology;
- 9 allowance settings;
- all 512 trust topologies;
- 6 mutants × 512 topologies.

`check.py` replays every Bend counterexample through `core.py`, step by step, to confirm that each one is a real attack.

The 3,072 mutant runs only need one fact: whether the mutant is caught. Both encodings therefore stop each of those runs at the first BFS level that shows a violation. When a mutant is not caught, the run still explores every state.

## 3. Leak counting (`qif.bend`)

Layer 2 tracks *which* secrets a value depends on. This layer counts *how much* the cloud learns. It uses self-composition: the system runs on 16 secret worlds at once (hr ∈ 0..3, acme ∈ 0..1, `.env` ∈ 0..1), and for every attack sequence it compares what the cloud observes across worlds. With `ca` and `cb` the bits charged so far, it checks the Köpf–Basin bounds:

- **A:** worlds that differ only in hr fall into at most 2^ca classes.
- **B:** worlds that differ only in acme fall into at most 2^cb classes.
- **S:** worlds that differ only in `.env` are never told apart.
- **T:** all 16 worlds fall into at most 2^(ca+cb) classes.

It checks every attack of up to 3 offloads: 189 actions per step, 338,845 traces with all bricks on.

## 4. Simulation (`sim.bend`)

This is a Monte Carlo of a working day, using an LCG that `sim_ref.py` reproduces bit for bit:

- 8,192 sessions per cell, 40 offloads per session.
- The task mix is assumed (45% public work, 35% private questions, 15% private documents, 5% sealed), not measured.

It shows what fraction of private questions get answered at each allowance and deadline.

## Limits

- **Small worlds.** The checks are complete only for the worlds above: 3 nodes, 2 scopes plus sealed, 2 result slots, and depth 3 for leak counting. A proof for any number of nodes or scopes needs an inductive argument, for example in Lean.
- **Taint abstraction.** Layer 2 assumes any dependency can be observed. Layer 3 removes that assumption for the small world.
- **Adapters are tested, not proved.** The sandbox room, route, door I/O and ssh transport are covered by `../tests/test_room.py` and the real-model run in `../tests/e2e.py`, not by these models.

## Found along the way

- **`core.check_policy` rejected every valid policy.** It called `label(p, (), …)`, which returns `sealed`. The brick tests caught it.
- **Bend 0.2.38 miscompiles a pattern.** Inside an if-branch that assigns two variables, `x = x + k` or `x = f(x)` keeps the old value. This happens on both `run-rs` and `run-c`. The repro is `bend_bug.bend`.
  - The cross-check caught it as 442 vs 735 states.
  - No Bend file here reassigns a name inside a branch.
