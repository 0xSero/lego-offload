# Verification

| File | What it does | Run |
|-|-|-|
| `Lego.lean` | Lean 4 proof, checked by Lean's kernel, for any policy, any number of nodes, scopes and calls, and any adaptive strategy. Reply times, the node queue and the prompt cache are in the model | `../run verify` |
| `twin.py` | Runs the Lean model and the real `Door.decide` on the same random calls and compares every decision | `../run verify` |
| `sim.bend` + `sim_ref.py` | [Bend](https://github.com/HigherOrderCO/Bend) Monte Carlo of a working day, reproduced bit for bit in Python: how useful the system is under budget and deadline | `../run sim` |

Results go to `../results/`.

## The thing being checked

`core.py` holds three types and seven bricks:

- `label`: a path gets its scopes. Unlisted paths are `sealed`.
- `move`: bytes go only to a node trusted with all of their scopes.
- `run`: the output keeps every input scope.
- `shape`: the answer comes from a finite set declared in advance, or is `UNKNOWN`.
- `charge`: bits are paid per scope before anything is read. Sealed is never chargeable.
- `clock`: the reply always comes at a fixed deadline.
- `queue`: one job at a time per model node. A private job that may start behind another job carries that job's scopes. A public job waits until the node is surely idle.

The cloud is a node trusted with nothing. It may call `offload(inputs, node, returns)` as often as it likes. `core.salt` keys each node's prompt cache by scope set, so a cache hit never links two scopes.

**A caller that sees some scopes.** With the two-way policy the caller (the agent's own model) may see a set T of scopes: some raw, some only scrubbed (a scrubbed twin such as `hr~` is a scope of its own, granted on its own). The door runs every brick on the scopes hidden from the caller, `scopes \ T`, so "public" in the model means "nothing hidden from the caller". That is a relabelling, and the theorems carry over unchanged. `leak_bound` for a set S of scopes disjoint from T fixes every secret outside S, including T's, so items labelled only with T scopes are constants in the compared worlds, exactly like public items in the model. So the caller learns at most Σ_{s∈S} allowance bits about S beyond what it may see. For scrubbed scopes, that means beyond the scrubbed view. `twin.py` checks the relabelling: a random T in every case, Lean given the hidden labels.

## What is proved (`Lego.lean`)

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

## Twin (`twin.py`)

`lean --run Lego.lean` executes the same `step` function that the proofs are about. `twin.py` generates 2,000 random cases: a host and up to three model nodes with random trust, a caller that sees a random set of scopes, random allowances and catalogs, and up to 14 calls at random times with random schemas, inputs and job lengths. For every call it compares the row, the output's hidden scopes, bits charged, ledger and the node's slot from Lean with those from the real `Door.decide`. All 14,695 calls agree. Deliberate bugs are caught: `<` turned into `≤` in `queue` (2 of 2,000 cases), `book` ignoring the old bound (278 of 2,000), and a door that ignores what the caller sees (74 of 400).

## Contention: why the queue exists

Without the queue, a free private job (a receipt) keeps generating in the background while a public job sent next shares the GPU with it. The public reply time is an observation the cloud gets for free, so it reveals how long the private job ran.

`../tests/contention.py` measures this on gpu-box. A private receipt job writes a 2,500-word essay if one person's salary is above 150,000 and one word otherwise. Three seconds later, a public job is sent to the same node. There are 8 trials per side:

| Door | Salary | Private job ran | Public reply (s), sorted |
|-|-|-|-|
| before the queue | above 150,000 | 21.6 to 28.4 s | 9.51 9.76 9.85 10.20 10.88 12.89 13.56 14.21 |
| before the queue | below | 0.3 to 0.5 s | 6.78 7.17 7.76 8.08 8.55 8.74 11.05 12.25 |
| with the queue | above 150,000 | 16.7 to 25.6 s | 123.16 124.61 125.00 125.08 126.17 127.00 129.78 131.95 |
| with the queue | below | 0.2 to 0.4 s | 121.83 122.30 124.94 126.44 127.21 129.17 129.97 129.97 |

| Door | Mean difference (above − below) | One-sided permutation p |
|-|-|-|
| before the queue | +2.56 s | 0.010 |
| with the queue | +0.12 s | 0.47 |

Before the fix, no single trial separated the two sides, but the attacker can repeat the pair of calls for free. With the queue, every public reply waits about 117 s, which is the 120 s limit minus the 3 s gap, and then runs alone. The spread that remains comes from the public job's own run time on a shared GPU. Raw data: `../results/contention_before.json` and `../results/contention_after.json`.

The cost is latency. A public job sent right after private work waits out that work's full time limit (`job_timeout`), however fast it really was. The wait depends only on labels and the clock, never on data.

## Simulation (`sim.bend`)

This is a Monte Carlo of a working day, using an LCG that `sim_ref.py` reproduces bit for bit:

- 8,192 sessions per cell, 40 offloads per session.
- The task mix is assumed (45% public work, 35% private questions, 15% private documents, 5% sealed), not measured.

It shows what fraction of private questions get answered at each allowance and deadline.

## Limits

- **A proof about a model.** `twin.py` ties the model to the door's decisions on random inputs, not on all inputs.
- **What the model assumes about the node.**
  - A job stops at its time limit: vLLM aborts a request whose client disconnects, and the runner's HTTP timeout is the limit.
  - Jobs on a node run one at a time, so a job's length depends only on its own prompt and its own cache hit.
  - The cache salt is collision-free per scope set.
  - Time is in whole units, and the door's own work takes no time.
- **Side channels outside the model.** The model covers reply rows, values and times as the door sends them. It does not cover the node's power draw or network traffic seen by third parties.
- **Adapters are tested, not proved.** The agent VM, route, agent adapters, door I/O and ssh transport are covered by `../tests/vmcheck.py`, `../tests/test_agents.py`, `../tests/test_queue.py` and the real-model runs in `../tests/matrix.py` and `../tests/twoway.py`.
