# Formal verification

Everything that checks the design lives here. Each check is written twice:

- once in [Bend](https://github.com/HigherOrderCO/Bend), from the spec and independent of `core.py`;
- once in Python, as a twin whose every decision is a call into the real `core.py`.

A checker script then asserts that the two agree on every number. A bug therefore has to land identically in two separate encodings before it can hide, and that is how a Bend compiler bug was found (see the end of this page).

Layers 1 to 5 are complete but bounded: every state of a small world. Layer 6 removes the bound. It is a Lean 4 proof, checked by Lean's kernel, for any number of nodes, scopes and calls, against any adaptive strategy, with reply times, the node queue and the prompt cache inside the model. It is then run against the real door on random call sequences.

Run it all with `../run verify` and `../run sim`. Results go to `../results/`.

| Layer | Bend | Python twin | Comparator | What it proves |
|-|-|-|-|-|
| 1. Brick laws | `lemmas.bend` | `lemmas_ref.py` | inside `lemmas_ref.py` | Each brick obeys its algebra on a full small domain |
| 2. Model check | `model.bend` | `model_ref.py` | `check.py` | Over every reachable state and every trust topology, no data reaches an untrusted node, and the cloud learns only what it paid for |
| 3. Leak counting | `qif.bend` | `qif_ref.py` | `qif_check.py` | On 16 concrete secret worlds, every attack of up to 3 offloads splits them into at most 2^(bits paid) classes per scope |
| 4. Simulation | `sim.bend` | `sim_ref.py` | inside `sim_ref.py` | How useful the system is under budget and deadline: bit-exact Monte Carlo |
| 5. Shared prompt cache | `cache.bend` | `cache_ref.py` | inside `cache_ref.py` | A node's prompt cache cannot make a public reply's time depend on private data, when it is salted by scope |
| 6. Unbounded proof | `Lego.lean` (proofs) | `twin.py` (the real `Door.decide`) | `twin.py` | For any policy and any strategy, after any number of calls the cloud learns at most Σ allowance bits about the scopes it targets, through answers, reply times, the queue and the cache together |

## The thing being checked

`core.py` holds three types and seven bricks:

- `label`: a path gets its scopes. Unlisted paths are `sealed`.
- `move`: bytes go only to a node trusted with all of their scopes.
- `run`: the output keeps every input scope.
- `shape`: the answer comes from a finite set declared in advance, or is `UNKNOWN`.
- `charge`: bits are paid per scope before anything is read. Sealed is never chargeable.
- `clock`: the reply always comes at a fixed deadline.
- `queue`: one job at a time per model node. A private job that may start behind another job carries that job's scopes. A public job waits until the node is surely idle.

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

`model.bend` takes a part number (`bend run-c model.bend 0..6`), because HVM 0.2.38 cannot read back the whole result in one go: the printed net comes out unterminated at about 130 KB. `check.py` reassembles the 7 parts.

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

## 5. Shared prompt cache (`cache.bend`)

Layers 2 and 3 treat the local model as a pure function. A real node keeps state between jobs: vLLM reuses cached prompt prefixes, so a job whose prompt starts like an earlier one gets its reply faster. Public jobs reply as soon as they finish (only shaped replies wait for the clock), so their reply time is an observation the cloud gets for free.

The attack this layer models:

1. Run a private job over hr with no `returns`. It costs nothing and replies at once with a receipt, but the node caches "task + hr file".
2. Run public jobs whose task text is that same task followed by a guess of the file. A faster reply means the guess is in the cache.

The world is one node, a secret hr value in 0..7 (8 worlds side by side), two task prefixes and eight guesses: 18 jobs per step, every sequence of up to 4 jobs (111,150 traces). The observation in each world is the hit bit of every public job. Noninterference holds when every trace leaves all 8 worlds with the same observations.

| Variant | Traces | Violating | Noninterference | Worlds told apart |
|-|-|-|-|-|
| salted by scope (`core.salt`) | 111,150 | 0 | holds | 1 |
| one shared cache (the mutant) | 111,150 | 21,376 | FAIL at 2 jobs | up to 4 |

The Python twin computes every cache key with the real `core.salt` and a fresh session key, and also checks that the salt separates all four scope sets and changes with the session key. Bend models the salt as the scope mask; it only needs to be injective.

This layer leaves out contention between jobs on the same node; layer 6 covers it. It also cannot cover a model that shares CPU or GPU with the room, which would let the agent time the model's work. The door refuses such a policy (a host with a model) instead.

## 6. Unbounded proof (`Lego.lean`)

### Why it was needed: contention, measured

Layers 2 to 5 treat jobs as if each ran alone. On a real node they do not. A free private job (a receipt) keeps generating in the background, and a public job sent next shares the GPU with it. The public reply time is an observation the cloud gets for free, so it can reveal how long the private job ran, and therefore what the private data said.

`../tests/contention.py` measures this on gpu-box. A private receipt job writes a 2,500-word essay if one person's salary is above 150,000 and one word otherwise. Three seconds later, a public job is sent to the same node. The results for the old door, with 8 trials per side, are in `../results/contention_before.json`:

| Salary | Private job ran | Public reply (s), sorted |
|-|-|-|
| above 150,000 | 21.6 to 28.4 s | 9.51 9.76 9.85 10.20 10.88 12.89 13.56 14.21 |
| below | 0.3 to 0.5 s | 6.78 7.17 7.76 8.08 8.55 8.74 11.05 12.25 |

The mean difference was 2.6 s, with a one-sided permutation p of 0.009. No single trial separates the two sides, but an attacker can repeat the pair of calls as often as it likes, for free. That is an uncharged channel, and the bounded models could not see it.

### The fix: the queue brick

Each model node runs one job at a time, first in, first out. The door keeps a slot per node: `(bound, scopes)`, where `bound` is the latest time the last booked job can end and `scopes` are that job's scopes.

- **A private job** arriving before the bound may start behind the previous job, so its timing depends on that job. Its output therefore carries the previous job's scopes too (`core.queue`). It is charged for them, and its reply is still a receipt or a shaped answer at the deadline. The slot moves to `max(t, bound) + time limit` (`core.book`).
- **A public job** carries nothing. It waits until the bound, when the node is surely idle, and then runs alone. The door holds the node's lock through the run, so no later decision is made on a stale slot.

The cost is latency: a public job sent right after private work waits out the private job's full time limit (`job_timeout`, 120 s in the tests), however fast the private job really was. That waiting time depends only on labels and the clock, never on data.

The same measurement on the fixed door, in `../results/contention_after.json`:

| Salary | Private job ran | Public reply (s), sorted |
|-|-|-|
| above 150,000 | 16.7 to 25.6 s | 123.16 124.61 125.00 125.08 126.17 127.00 129.78 131.95 |
| below | 0.2 to 0.4 s | 121.83 122.30 124.94 126.44 127.21 129.17 129.97 129.97 |

| Door | Mean difference (above − below) | One-sided permutation p |
|-|-|-|
| before the queue | +2.56 s | 0.010 |
| with the queue | +0.12 s | 0.47 |

Every public reply now waits about 117 s, which is the 120 s limit minus the 3 s gap, and then runs alone. The spread that remains, about 122 to 132 s, comes from the public job's own run time on a shared GPU. It is the same on both sides.

### What is proved

The model is a state machine over symbolic secrets:

- A `World` gives every scope a secret value. Every value in the model (item contents, outputs, job lengths, finish times, cache hits) is a function of the world.
- Each call's decisions (row, scopes, charge, slot) depend only on labels, the ledger and times, which `next_inv` shows are world-independent.
- The strategy is any function from the history of observations to the next call, so it adapts.
- An observation is a refusal, a receipt with its time, a full output with its finish time, or a shaped answer with its reply time.
- Job lengths are arbitrary functions of the prompt and of whether the prompt cache hit.

The main theorems hold for every policy, catalog, strategy and number of calls:

- `leak_bound`: let S be any set of scopes, and fix the secrets outside S. The cloud's whole history is a function of a code below 2^(Σ_{s∈S} allowance s). So it can tell apart at most that many worlds, which is at most Σ allowance bits about S, counting answers, reply times, the queue and the cache together.
- `sealed_hidden`: worlds that differ only in the sealed scope give identical histories.
- `held_trusted`: every copy and output is on a model node trusted with all of its scopes, and never holds sealed data, given a well-formed policy.
- `budget`: spent plus remaining equals the allowance for every scope, and sealed is never charged.
- `reply_times`: every reply is a refusal, an immediate receipt, a full public output, or a shaped answer exactly `deadline` after its call.

The proof is an invariant (`Inv`) plus a digit argument. Each charged answer adds a digit below 2^bits to a mixed-radix code, and two runs with the same code have the same history (`runS_prefix`).

**Every rule is load-bearing.** For each rule switched off, `decide` evaluates a concrete two-world counterexample that the real door blocks:

| Rule off | Counterexample |
|-|-|
| `queue` | a public job's finish time reveals hr, with zero allowance |
| `salt` | a public job's cache hit reveals hr, with zero allowance |
| `clock` | with one bit and the same answer and code in both worlds, the reply time reveals hr |
| `charge` | hr comes back as an answer with zero allowance |
| `shape` | an answer outside its schema exceeds 2^(bits paid) |
| `run` | a laundered output comes back in full |
| `move` | hr bytes land on a node not trusted with hr |

`../run verify` compiles the file and prints the axioms each theorem uses. The main theorems use only `propext`, `Classical.choice` and `Quot.sound`, the counterexamples only `propext`, and none uses `sorry`. Lean 4 core only, no Mathlib.

### Twin (`twin.py`)

`lean --run Lego.lean` executes the same `step` function that the proofs are about. `twin.py` generates 2,000 random cases: a host and up to three model nodes with random trust, random allowances and catalogs, and up to 14 calls at random times with random schemas, inputs and job lengths. For every call it compares the row, output scopes, bits charged, ledger and the node's slot from Lean with those from the real `Door.decide`. All 15,092 calls agree. Two deliberate bugs in `core.py` were each caught: `<` turned into `≤` in `queue` (7 cases), and `book` ignoring the old bound (398 cases).

## Limits

- **Small worlds (layers 1 to 5).** Those checks are complete only for the worlds stated. Layer 6 has no such bound, but it proves a model; the twin ties that model to the door's decisions on random inputs, not on all inputs.
- **What the Lean model assumes about the node.**
  - A job stops at its time limit: vLLM aborts a request whose client disconnects, and the runner's HTTP timeout is the limit.
  - Jobs on a node run one at a time, so a job's length depends only on its own prompt and its own cache hit.
  - The cache salt is collision-free per scope set.
  - Time is in whole units, and the door's own work takes no time.
- **Side channels outside the model.** The model covers reply rows, values and times as the door sends them. It does not cover the node's power draw, network traffic seen by third parties, or the room itself (the sandbox is tested, not proved).
- **Taint abstraction.** Layer 2 assumes any dependency can be observed. Layer 3 removes that assumption for the small world.
- **Adapters are tested, not proved.** The sandbox room, route, agent adapters, door I/O and ssh transport are covered by `../tests/test_room.py`, `../tests/test_agents.py` and the real-model run in `../tests/e2e.py`, not by these models.

## Found along the way

- **Contention between jobs on one node** (layer 6, above): measured on gpu-box, fixed with the queue brick, then proved.
- **`core.check_policy` rejected every valid policy.** It called `label(p, (), …)`, which returns `sealed`. The brick tests caught it.
- **Bend 0.2.38 miscompiles a pattern.** Inside an if-branch that assigns two variables, `x = x + k` or `x = f(x)` keeps the old value. This happens on both `run-rs` and `run-c`. The repro is `bend_bug.bend`.
  - The cross-check caught it as 442 vs 735 states.
  - No Bend file here reassigns a name inside a branch.
- **HVM's parallel C runtime sometimes leaves one term unreduced.** On two runs of the same part, a different single topology (407, then 475) came back as free variables (`$me, $ne, …`) instead of numbers.
  - It never produces a wrong number, and every number is compared with `core.py` anyway.
  - `../run` retries such a part up to twice, and `check.py` takes each entry from the first attempt that reduced it.
  - An entry that is still unreduced is reported as `hvm_unreduced` and fails the check.
