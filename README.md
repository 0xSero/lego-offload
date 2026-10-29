# lego-offload

Let a cloud coding agent (Claude Code) work with private data it is never allowed to see.

- The agent runs in a **sealed room**, a macOS sandbox. Inside it can reach its workspace and two localhost ports, and nothing else.
- Private data stays on the machines trusted with it, here a GPU box called `gpu-box`. A **local model** on that machine does the private work.
- The agent gets one tool, `offload`. What comes back is decided by six small rules, the **bricks**:
  - **Public inputs:** the full output.
  - **Private inputs:** a small typed answer (an int, a bool or an enum), paid for in bits from a per-scope allowance and delivered at a fixed time.
  - **Anything else:** a receipt.

The design is checked four ways in Bend, each cross-checked against the real code: brick laws, an exhaustive model check, leak counting and a simulation. It is also tested end to end with real models.

```
 ┌──────────── Mac (host, trusted with everything) ─────────────────────────────┐
 │                                                                              │
 │  ┌──────── room (sandbox-exec, deny by default) ─────────┐                   │
 │  │  Claude Code    workspace/ (public files only)        │                   │
 │  │      │  model calls        │ offload(task, inputs, returns)               │
 │  └──────┼─────────────────────┼──────────────────────────┘                   │
 │         ▼                     ▼                                              │
 │     route :18471           door :18472 ── core.py: label → charge → move → run│
 │  (injects the OAuth    (the only tool)          │                            │
 │   token; captures         ▲ shaped answer       │ ssh                        │
 │   every request)          │ at the deadline     ▼                            │
 └─────────┼─────────────────┼──────────────────────────────────────────────────┘
           ▼                 │          ┌──────── gpu-box (trusted: hr, acme) ─────┐
     api.anthropic.com       └───────── │  /data/hr  /data/acme   local model     │
     (trusted: nothing)                 │  full outputs stay here: out/<ref>.txt  │
                                        └─────────────────────────────────────────┘
```

## One command

```
./run            # everything: unit, verify, sim, e2e, then results/REPORT.md
./run unit       # brick tests + the sandbox probed with plain binaries (no model)
./run verify     # Bend: brick laws, model check over 512 topologies, leak counting
./run sim        # Bend Monte Carlo: answered rate vs allowance and deadline
./run e2e        # real models: private data on gpu-box, Claude Code in the room
```

Requirements:

- macOS for `sandbox-exec`.
- Python 3.10+, standard library only.
- [Bend](https://github.com/HigherOrderCO/Bend) 0.2.38 (`bend run-c`).
- Claude Code logged in. The route reads its OAuth token from the keychain, and the token never enters the room.
- For `e2e`: an ssh alias `gpu-box` with an OpenAI-compatible model on its `127.0.0.1:8000`.

Every step runs under a hard timeout. `e2e` puts synthetic data with canaries on gpu-box and deletes it afterwards.

## How you use it

**1. Write a policy.** It names the machines, what each may hold, which directories are private, and how many bits per scope the agent may learn. See [`examples/policy.json`](examples/policy.json):

```json
"nodes": {
  "mac":   {"trust": ["hr", "acme", "legal", "sealed"]},
  "pop":   {"trust": ["hr", "acme"], "ssh": "gpu-box",
            "model": {"base_url": "http://127.0.0.1:8000/v1", "model": "deepseek-v4.1-flash-1017"}},
  "cloud": {"trust": []}
},
"scopes":    {"pop:/srv/data/hr": "hr", "pop:/srv/data/acme": "acme", "mac:/Users/you/legal": "legal"},
"public":    ["/Users/you/code/app/README.md", "/Users/you/code/app/src/utils.py"],
"allowance": {"hr": 16, "acme": 16, "legal": 16},
"deadline":  30
```

Anything not listed is `sealed`: no node with a model may hold it, and no allowance can buy it.

**2. Run your agent through lego** instead of directly:

```
python3 lego.py examples/policy.json -p "How many people in hr earn over 150k?" --output-format json
python3 lego.py examples/policy.json --reset          # refill the allowances
```

This starts the door and the route, launches Claude Code in the room with the `offload` tool, and stops everything when the agent exits.

**3. What the agent actually does.** These are real calls from the e2e run, copied from the route's capture of what Claude Code sent and received:

```
→ offload(task="Count the employees whose salary is strictly greater than 150000.",
          inputs=["pop:/tmp/lego-e2e-…/hr/salaries.csv"], returns={"int": [0, 100000]})
← {"refused": "answer carries 17 bits; the limit is 16"}

→ offload(task="…same…", inputs=[…salaries.csv], returns={"int": [0, 1000]})
← {"row": "shaped", "answer": 7, "ref": "pop:/tmp/…/out/ba87….txt", "allowance_left": {"hr": 6, …}}

→ offload(task="How many days of notice are required for termination?",
          inputs=["pop:/tmp/lego-e2e-…/acme/contract.md"], returns={"int": [0, 365]})
← {"row": "shaped", "answer": 95, "ref": "…", "allowance_left": {"acme": 7, …}}
```

The agent never saw a salary or a line of the contract. It saw `7` and `95`, each arriving exactly 30 s after the call, and each paid for: 10 bits from hr and 9 bits from acme.

Reply rows:

- **Public inputs** return the full output.
- **Private inputs without `returns`** give a receipt. The full output is saved on gpu-box for you, and its ref can be passed to later offloads.
- **Sealed inputs** (`.env`) are refused, and so is the Mac-only `legal` scope, because no machine with a model is trusted with it.

## The bricks (`core.py`)

| Brick | Rule |
|-|-|
| `label(path)` | A listed directory gets its scope. Unlisted or odd paths (`..`, non-NFC, relative) are `sealed` |
| `move(item, node)` | The only way bytes cross machines. Allowed only if the node trusts every scope on the item |
| `run(inputs, node)` | Work runs where its inputs are. The output keeps every input scope |
| `shape(value, schema)` | One value from a finite set declared before the job runs (bool, enum, int range, tuple), else `UNKNOWN` |
| `charge(ledger, scopes, bits)` | Pays ⌈log2(size+1)⌉ bits from every scope involved, before anything is read. Sealed has no allowance |
| `clock(value, took, deadline)` | The reply comes at the deadline, whatever the job did. A late job answers `UNKNOWN` |

The reply row is chosen from labels and the ledger alone, before any private byte is read. `release_plan` and `deliver` compose the last three bricks.

## Files

| File | Lines | What |
|-|-|-|
| `core.py` | 175 | The six bricks. Pure: no I/O, no clock, no network |
| `door.py` | 175 | The `offload` tool, served as MCP over HTTP on localhost. Asks `core.py` at every step |
| `transport.py` | 80 | Carries out moves and runs over ssh. Calls the node's own model with constrained JSON decoding |
| `route.py` | 97 | Localhost proxy to the cloud model. Strips the room's dummy key, injects the real one, can capture every request |
| `room.py` + `room/room.sb` | 37 + 110 | The sandbox. Deny by default, with the workspace, a throwaway HOME, and the two ports allowed. Refuses planted hardlinks |
| `lego.py` | 51 | The CLI: door + route + room around one agent session |
| **implementation** | **725** | |
| `verify/` | | Formal verification in Bend, with Python twins. See [`verify/README.md`](verify/README.md) |
| `tests/` | | `test_core.py` (bricks), `test_room.py` (sandbox), `e2e.py` (real models), `report.py` |
| `results/` | | Output of the last `./run`, including `REPORT.md` |

## Results

RESULTS_PLACEHOLDER

## What it defends against, and what it doesn't

**The agent cannot:**

- read files outside its workspace, reach the network beyond the route and the door, use ssh, read the keychain, or run deputies (`osascript`, `open`, `launchctl`): the sandbox blocks these, as checked in `tests/test_room.py`;
- get private bytes through the door: replies are full only when every input is public;
- learn more about a scope than its allowance: the ledger is charged before the job runs, and the model check and leak counting prove the bound;
- use timing: shaped replies arrive at a fixed deadline;
- name its way around the labels: unlisted paths are sealed, and policies with a public path inside a scope are refused.

**Not covered:**

- **Bits are bits.** 16 bits about hr can be the single bit that matters most. The allowance bounds the quantity, not the meaning, so set it per scope with that in mind.
- **The host is trusted.** All bytes between nodes pass through the Mac, and policy requires the Mac to be trusted with every scope.
- **Sandbox scope.** The sandbox is macOS `sandbox-exec`. Its boundary is tested by probes, not proved.
- **Local model quality.** If the local model miscounts, the answer is wrong but still shaped.
- **Proof size.** The formal results cover small worlds, not any number of nodes. See [`verify/README.md`](verify/README.md#limits).
