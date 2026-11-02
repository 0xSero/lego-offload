---
name: lego
description: Explain and operate lego, the private-data router for coding agents — run Claude Code or omp through lego, write or check a policy, read the door's refusals, run the checks, and test a setup for leaks. Use when asked "how does lego work", "set up lego", "run claude/omp through lego", "why was this refused", "is anything leaking", "test lego", or when editing the policy. Not for changing lego's own code (read README.md and tests/integration.py for that).
---
# lego

## What it is

lego runs a coding agent (Claude Code or omp) in a Linux VM that holds only its workspace. Private folders stay on
the Mac. The agent reaches them only through two MCP tools served by the **door** on the Mac: `read` and `offload`.
The **route** on the Mac is the agent's model proxy: it adds the real credential, so the VM never holds a key.

Every model has a level per private folder (a *scope*):

| Level | The model gets |
|-|-|
| `everything` | the raw files |
| `scrubbed` | names, emails, phones, addresses, IDs, keys swapped for placeholders; numbers, dates and sentences stay |
| `nothing` | nothing |

## The rules, as a tree

```
START   lego claude → agent = Claude (cloud)    lego omp → agent = omp (local model)
├─ refuse if a private folder overlaps the workspace or lego's state
├─ refuse if a workspace file is a hard link to a private file
├─ refuse if a model on this Mac (no ssh) would get data the agent can't see
└─ start the VM, the door and the route

read(file)
├─ workspace file             → as is
└─ private file, agent level: everything → raw | scrubbed → scrubbed copy | nothing → refused

offload(task, inputs, returns?, node?)        the job goes to another model
├─ inputs cross at THAT model's level: everything → raw | scrubbed → scrubbed | nothing → refused
└─ reply to the agent
   ├─ the agent could see every input     → full answer
   ├─ it asked for `returns` (typed)       → that small answer only, at the fixed deadline, paid in bits
   └─ otherwise                            → receipt: the user reads it with `lego show <ref>`
```

Default: the local model sees `everything`, the cloud sees `nothing`. So `lego claude` gets typed answers or
receipts about private data and never the data; `lego omp` reads it raw and cannot send it to the cloud.

## Set up

1. Apple `container` installed (`container system status`), Claude Code logged in on the Mac for `lego claude`.
2. A local model reachable by URL **and** by ssh on another machine (`ssh <host> true` must work with no prompt),
   with a writable `dir` there for jobs.
3. Policy at `~/.lego/policy.json` (or `./lego.json`, `$LEGO_POLICY`, `--policy FILE`). Start from
   `examples/policy.json`:

```json
{
  "workspace": "/path/to/project",
  "private": {"hr": "/path/to/private/hr", "keys": "/path/to/private/keys"},
  "allowance": 16,
  "deadline": 60,
  "models": {
    "local": {"url": "http://HOST:8000/v1", "model": "MODEL", "ssh": "HOST", "dir": "/srv/lego", "sees": "everything"},
    "cloud": {"api": "anthropic", "model": "claude-sonnet-5-5", "sees": "nothing"}
  }
}
```

| Field | Meaning |
|-|-|
| `workspace` | the folder mounted in the VM (default: where you run lego). Must not overlap a private folder |
| `private` | scope name → folder. Never inside the workspace or each other |
| `public` | optional files the agent may read raw |
| `allowance` | bits per scope that typed answers may spend (16 = one 4-digit number). Lower it for sensitive folders |
| `deadline` | seconds every typed answer waits. Must exceed the local model's job time, or answers come back `UNKNOWN` (still charged) |
| `job_timeout` | seconds a receipt or full job may run |
| `sees` | `everything`, `scrubbed`, `nothing`, or per scope: `{"hr": "scrubbed", "keys": "nothing"}` |

## Operate

```
lego claude                                   # interactive, Claude in front
lego claude -p "average salary per dept?"     # one shot
lego omp                                      # the local model in front
lego show <ref>                               # read a receipt's result, on the Mac only
lego reset                                    # refill every scope's allowance
lego reset --workspace                        # forget private data an earlier session left in the workspace
lego app [PORT]   |   lego app --demo         # web page; --demo uses synthetic data
```

What happened in a session: `.lego/door.jsonl` next to the policy, one line per `read`, `offload`, `answer`,
`refused`, `error`. Jobs sent to the cloud: `.lego/cloud/out/*.trace.json`.

## Refusals and what to do

| Message contains | Meaning | Fix |
|-|-|-|
| `you may not see [...]; ask about it with offload` | agent's level is `nothing` | use `offload` with `returns`, or raise the level |
| `no model may be given [...]` | no model may receive those inputs | give one model that scope, or don't send it |
| `Your context holds X from what you read` | omp read X; every task now carries X | do the work locally, or start a new workspace |
| `give X ssh to another machine` | a local model on this Mac would get hidden data | add `ssh` + `dir` for that model |
| `is a hard link to a private file` | a workspace file is a private file | delete the link |
| `workspace overlaps a private directory` | workspace and a private folder nest | move one of them |
| `the workspace holds [...] data from an earlier session` | an earlier agent wrote private data there | clear it, then `lego reset --workspace` |
| `not enough allowance` / reply is a receipt | the scope's bits are spent | `lego reset`, or ask for a smaller `returns` |
| answer `UNKNOWN` | the job missed the deadline or answered off-schema | raise `deadline`; check `door.jsonl` for `error` |
| `is sealed` | the path is in no private folder, not public, not in the workspace | name a listed file (`read "."` lists them) |

## Check it

```
./run integration    # the real door on real files, stub model; no network, ~5 min
./run verify         # Lean proof of the leak bound + the model checked against the door
./run vm             # every connection from inside the VM; no credential reaches it
./run e2e            # real agents and models at every level, both ways, canaries scanned (needs the local model)
./run                # all of the above
```

## Test a setup for leaks

1. Make a test folder with `private/` (a file of canaries: a name, email, phone, address, key) and `workspace/`,
   and a policy pointing at them. Never use real secrets you can't rotate.
2. Run the agent with the route capturing every exchange: `lego.session(policy, state, "claude", argv,
   capture="route.jsonl")` from Python, as `tests/e2e.py` does.
3. Ask it to read the file, then to offload. Then script the attacks yourself: agents refuse to red-team, so call
   `door.Door(policy, state, caller)` → `.prepare(lego.finder(policy), strict=True)` → `.read(path)` /
   `.offload(task, inputs, returns, node)` directly.
4. Scan `route.jsonl` (Claude's traffic) and `.lego/cloud/out/*.trace.json` (cloud jobs) for every canary, also
   reversed, base64, hex, digits only and spelled out.

Known limits (by design, see README "Not covered"):
- `scrubbed` keeps dates, numbers and sentences: a birth date or medical note crosses. Use `nothing` for those.
- With `lego omp` and a `scrubbed` cloud, the local agent can spell data out on purpose ("eight one four…").
- Bits are bits: 16 bits buys a 4-digit number. Size `allowance` per scope.
- A plain copy of a private file placed in the workspace is not detected. Don't put one there.
