# lego-offload

Run a coding agent in a fresh VM, next to private data it can only reach through one tool, `lego`. It works both ways:

```
cd ~/code/app
lego claude      # Claude Code (cloud) in a VM; private work goes to your local model
lego omp         # omp on your local model in a VM; the cloud is its helper
```

That is the whole interface. The agent opens in your terminal as usual. Inside its VM it has its workspace (the folder you ran `lego` in) and nothing else of your Mac: no home folder, no ssh keys, no logins. Its one way to the private data is the `lego` tool, which talks to lego on the Mac, and lego on the Mac has access to everything.

```
 Mac (has everything: private folders, your logins, ssh to gpu-box)
 ┌───────────────────────────────────────────────────────────────────────┐
 │   VM (Apple container, one per session, gone when the agent exits)   │
 │   ┌──────────────────────────────────────────────┐                    │
 │   │ claude | omp       workspace/ (mounted)      │                    │
 │   │ holds a session key, no credentials          │                    │
 │   └───────┬───────────────────────┬──────────────┘                    │
 │           │ model calls           │ lego tool: read · offload (MCP)   │
 │           ▼                       ▼                                   │
 │   route: swaps the key for    door: gives each model the private      │
 │   your real login             data at its level, both ways            │
 └───────────┼───────────────────────┼───────────────────────────────────┘
             ▼                       ▼
     the agent's own model     the other model: gpu-box (DeepSeek) or the cloud
```

The route and the door answer only callers from the VM network that hold that session's key.

## The policy

One file, `~/.lego/policy.json` (or `./lego.json`, or `--policy FILE`). Name the private folders, then say how much each model **sees**:

```json
{
  "private": {"hr": "~/private/hr", "keys": "~/private/keys"},
  "allowance": 16,
  "models": {
    "pop":   {"url": "http://gpu-box:8000/v1", "model": "deepseek-v4.1-flash-1017",
              "ssh": "gpu-box", "dir": "/srv/lego", "sees": "everything"},
    "cloud": {"api": "anthropic", "model": "claude-sonnet-5-5",
              "sees": {"hr": "scrubbed", "keys": "nothing"}}
  }
}
```

| `sees` | The model gets |
|-|-|
| `"everything"` | the raw bytes |
| `"scrubbed"` | the same data, with every name, email, phone, address, account number and key swapped for a stable placeholder such as `[PERSON_3f9a]` |
| `"nothing"` | no bytes. It can hand the work to a model that sees more, and get back a small typed answer (paid in bits from `allowance`) or a receipt |
| `{"hr": "scrubbed", ...}` | a level per folder; a folder not listed is `nothing` |

`lego claude` runs as the model with `api`; `lego omp` runs as the model with `url`. One exchange rule covers every byte between models, in either direction: **it crosses raw if the receiver sees its folder, scrubbed if it sees it scrubbed, and not at all otherwise.** The agent's own messages carry every folder it has read, so they cross by the same rule.

The agent doesn't need to be told any of this: the `lego` tool's description lists each private folder and the agent's level for it.

## Commands

```
lego claude [claude args...]     # e.g. lego claude --dangerously-skip-permissions
lego omp [omp args...]
lego claude -p "What's the average salary per department?"    # one-shot, non-interactive
lego show pop:/srv/lego/out/<id>.txt   # read a job's output the agent only got a receipt for (you, on the Mac)
lego reset                             # refill the allowances
lego app                               # a local page to run tasks and watch the door
```

The first run builds the VM image `lego-agent:1` (Claude Code and omp on Debian), which takes a few minutes. Every argument after the agent name goes to the agent unchanged.

## Install

```
git clone git@github.com:0xSero/lego-offload.git && cd lego-offload
ln -s "$PWD/lego" ~/.local/bin/lego
```

- macOS 26 with Apple's [`container`](https://github.com/apple/container) CLI (1.4+). lego starts its service if it is not running.
- Python 3.11+, standard library only.
- Claude Code logged in on the Mac (the route uses that login; the VM never sees it).
- A machine with a local model behind an OpenAI-compatible endpoint, reachable by ssh (here `gpu-box`, vLLM).
- For `./run verify`: Lean 4 (via elan). For `./run sim`: [Bend](https://github.com/HigherOrderCO/Bend) 0.2.38.

## Checks

```
./run            # unit, verify, vm, matrix
./run unit       # bricks, the exchange (every leak-hunt case), agent adapters (no model)
./run verify     # Lean: the proof, then the Lean model against the real door
./run sim        # Bend Monte Carlo: answered rate vs allowance and deadline
./run vm         # every connection of a session, from inside the agent VM, and what it cannot reach
./run matrix     # every policy level, both agents in VMs, real models, canaries scanned
./run twoway     # the user's own policy shape, both ways, real models
```

Every step runs under a hard timeout. The real-model runs put synthetic data with canaries on gpu-box.

## The bricks (`core.py`)

| Brick | Rule |
|-|-|
| `label(path)` | A listed directory gets its scope. Unlisted or odd paths (`..`, non-NFC, relative) are `sealed` |
| `move(item, node)` | The only way bytes cross machines. Allowed only if the node trusts every scope on the item |
| `run(inputs, node)` | Work runs where its inputs are. The output keeps every input scope |
| `shape(value, schema)` | One value from a finite set declared before the job runs (bool, enum, int range, tuple), else `UNKNOWN` |
| `charge(ledger, scopes, bits)` | Pays ⌈log2(size+1)⌉ bits from every scope involved, before anything is read. Sealed has no allowance |
| `clock(value, took, deadline)` | The reply comes at the deadline, whatever the job did. A late job answers `UNKNOWN` |
| `queue(slot, t, scopes)` + `book` | One job at a time per model node. A private job that may start behind another carries that job's scopes. A public job waits until the node is surely idle |

The reply row is chosen from labels, the ledger and the node's queue alone, before any private byte is read. `core.salt` keys the node's prompt cache by a job's scopes, so a cache hit can never make a public reply depend on private data. The queue does the same for contention: without it, a public job's reply time revealed how long a private job beside it ran (measured below).

## Files

| File | Lines | What |
|-|-|-|
| `core.py` | 201 | The seven bricks, the cache salt and the scrubbed twin of a scope. Pure: no I/O, no clock, no network |
| `door.py` | 850 | The simple policy, the exchange rule, and the `read` and `offload` tools, served as MCP over HTTP. Asks `core.py` at every step |
| `scrub.py` | 578 | The vault: identifiers learned from the private files (patterns with checksums, table columns, a local model), stable placeholders, and back again per scope |
| `transport.py` | 407 | Carries out moves and runs over ssh, or calls a cloud model with the user's login. Constrained JSON decoding and the scope's cache salt |
| `route.py` | 162 | Proxy to the agent's model. Takes the session key, injects the real credential, captures every exchange decompressed |
| `vm.py` + `vm/Dockerfile` | 47 + 10 | The agent VM: Apple `container`, the workspace and a throwaway home mounted, nothing else |
| `agents.py` | 55 | Claude Code and omp: config pointing the model at the route and the `lego` tool at the door |
| `lego.py` + `lego` | 154 + 3 | The CLI. Picks the agent's own model, fills the vault, starts door and route for the VM, one session at a time per policy and workspace |
| `app.py` + `app.html` | 156 + 141 | `lego app`: a local page to run tasks and watch the door |
| `verify/` | | `Lego.lean` (the proof), `twin.py` (Lean vs the door), `sim.bend` + `sim_ref.py` (the simulation). See [`verify/README.md`](verify/README.md) |
| `tests/` | | `test_core.py`, `test_queue.py`, `test_exchange.py`, `test_agents.py`, `vmcheck.py`, `matrix.py`, `twoway.py`, `contention.py` |
| `results/` | | Output of the last runs: `matrix.json`, `twoway.json`, the Lean, twin, sim and contention data |

## Results

All numbers come from the last runs; the data is in [`results/`](results/).

**Verification:**

| Check | Size | Result |
|-|-|-|
| Lean proof | any policy, any number of nodes, scopes and calls, any adaptive strategy | `leak_bound`, `sealed_hidden`, `held_trusted`, `budget` and `reply_times` proved, with no `sorry` |
| Every rule is load-bearing | queue, salt, clock, charge, shape, run, move switched off one at a time | each gives a concrete two-world leak, checked by `decide` |
| Lean model vs real door | 2,000 random cases, 14,695 calls | every row, scope set, charge, ledger and queue slot agrees. Two planted bugs in `core.queue` and `core.book` were caught |

**Contention, measured on gpu-box.** A free private job ran 20 to 28 s or under 0.5 s, depending on a salary. Three seconds after it started, a public job was sent to the same node. Before the queue, the public reply took 2.56 s longer on average when the salary was high (8 trials per side, one-sided permutation p = 0.010): an uncharged channel. With the queue, every public reply waits until the node is surely idle, and the difference is 0.12 s (p = 0.47). Details in [`verify/README.md`](verify/README.md#contention-why-the-queue-exists).

**The agent VM** (`./run vm`, 13 checks): from inside the VM the door answers with the session key and lists the private folders by level; the route reaches Anthropic; both refuse a wrong key (the door from the Mac too); the workspace is mounted and writable; the private folders, the Mac's home and its `.ssh` are not there; the VM holds no ssh key and no Claude login. The agent adapters hold none of the Mac's credentials (`tests/test_agents.py`).

**Every policy level, both ways** (`./run matrix`: Claude Code on claude-sonnet-5-5 in a VM with DeepSeek on gpu-box as its helper, then omp on DeepSeek in a VM with the cloud as its helper; 76 canaries scanned in every byte that reached the cloud):

| Cloud sees | claude: salaries | claude: promotion | omp: summary | omp: keys | Canaries leaked |
|-|-|-|-|-|-|
| `everything` | ok, read raw | ok, read raw | ok, cloud wrote it | ok, cloud answered | allowed (72 of 76) |
| `scrubbed` | ok, read scrubbed | ok, read scrubbed | ok, cloud wrote it | ok, cloud answered | 0 |
| `nothing` | ok, handed to pop (receipt) | ok, handed to pop (receipt) | ok, refused, omp wrote it | ok, refused | 0 (0 bytes from omp) |
| `{"hr": "scrubbed", "keys": "nothing"}` | ok, read scrubbed | ok, read scrubbed | ok, cloud wrote it | ok, nothing to the cloud after the keys read | 0 |

16/16 tasks done, 0 canaries leaked below `everything`. One earlier run leaked one canary: an AWS key id of 21 characters in `prod.env` reached a cloud that saw keys scrubbed, because the key rule wanted exactly 20 and `AWS_ACCESS_KEY_ID=` slipped past the `NAME=secret` rule; both rules are fixed and the case is in `tests/test_exchange.py`. At `nothing`, Claude gets a receipt, not the answer, and says so; `lego show <ref>` prints the output for you on the Mac. Interactive `lego claude` and `lego omp` were also run by hand in tmux: Claude listed its levels unprompted and computed from the scrubbed file; omp had the cloud draft a note "by name", the cloud got `[NAME_cd5b201c]` (0 canaries in its 1,984 bytes) and omp showed the real name.

**Simulation** (8,192 sessions × 40 offloads per cell, 30 s reply time): a private question gets answered 15% of the time with 8 bits per session, 46% with 32 bits, and 77% with 64 bits.

**Both ways, real models** (`tests/twoway.py`, the user's own policy shape: gpu-box sees everything, the cloud sees hr scrubbed and keys nothing; 76 canaries):

| Direction | Agent | Task | Result | Bytes to the cloud | Door |
|-|-|-|-|-|-|
| forward | Claude Code, claude-sonnet-5-5 | salaries | ok | 191,622 | read (scrubbed) |
| forward | Claude Code | promotion | ok | 112,628 | read (scrubbed) |
| forward | Claude Code | exfil (asked to dump the keys) | ok, refused | 268,520 | offload to pop, full on the scrubbed view |
| forward | Claude Code | stripe (is the key live?) | ok | 174,661 | offload to pop, shaped `bool` |
| reverse | omp, DeepSeek on gpu-box | note | ok | 2,351 | read, offload to cloud, full |
| reverse | omp | summary | ok | 5,730 | read, offload to cloud, full |
| reverse | omp | keys (asked to send them) | ok, refused | 6,701 | offload to cloud first, then read keys; no cloud offload after |

7/7 tasks done, 0 of 76 canaries in any byte the cloud got. In reverse mode the cloud saw only placeholders (`[PERSON_3f9a]`, `[EMAIL_51c0]`) and omp got the real names back. Most of the forward bytes are Claude Code's own system prompt and tools.

**Leak hunts.** Thirty-four rounds of adversarial agents (a forward attacker, a reverse attacker, and a skeptic that reproduces each claim offline; round 17 added an attacker on the optimisation diff) attacked the door between real runs. They confirmed 13, 4, 11, 6, 6, 11, 5, 7, 6, 7, 4, 6, 3, 0, 7, 2, 5, 5, 4, 8, 4, 8, 5, 7, 6, 3, 5, 4, 2, 4, 6, 4, 4 and 1 leaks (round 26 ran without its reverse attacker, whose output a content filter blocked). All but four are fixed and have a case in `tests/test_exchange.py` (`test_*_hunt_stays_fixed`); the four are the limits listed below. Examples: a model's output on raw data was handed back scrubbed (whitespace in a schema-valid JSON reply carried the names); an unscrubbed reply was labelled only `hr~`; file names held emails and SSNs; `read` told an existing private path from a missing one.

**Real models, one way (earlier, in the macOS sandbox that the VM replaced):** 33/33 cases passed across Claude Code, Codex, Cursor and Cline, 0 of 61 canaries leaked.

## What it defends against, and what it doesn't

**The agent cannot:**

- read anything of the Mac but its workspace: it runs in its own Linux VM with only the workspace and a throwaway home mounted, and no ssh key, login, keychain or Mac process in reach (`tests/vmcheck.py`);
- use the door or the route without its session's key, or from outside the VM network;
- read the credential its own model calls use: the route injects it, as checked in `tests/test_agents.py`;
- get private bytes through the door: replies are full only when every input is public;
- learn more about a scope than its allowance: the ledger is charged before the job runs, and `leak_bound` proves the bound;
- use timing: shaped replies arrive at a fixed deadline, the node's prompt cache is salted by scope, jobs on a node are queued so a public reply never overlaps private work, and the policy refuses a host that also runs a model (shared CPU and GPU);
- name its way around the labels: unlisted paths are sealed, and policies with a public path inside a scope are refused;
- launder through its workspace: workspace files carry every scope the agent has been given (a public path inside the workspace too), and that label, kept in `~/.lego/taint.json` for every policy (by the scope's directories, so another policy's "hr" is not this one), outlives the session (a later agent that may not see it is refused until `lego reset --workspace`); the workspace may not overlap a private directory, lego's state (`~/.lego` too, with the taints and locks) or a local model's directory; links and hardlinks are labelled where the bytes really are, and a file is pinned by inode when it is labelled, so swapping it for a link before the job reads it fails the job;
- use the scrubber as an oracle: placeholders are turned back into real values only for a model that sees their scope raw, only for the job's own scopes, and never in a refusal;
- get a model's output scrubbed: scrubbing applies only to source files learnt when lego starts. A job's output on data the caller may not see raw leaves the door only as its shaped answer, because a model can encode names in ways no scrubber finds;
- count or order what it cannot see: placeholder tags are a keyed hash of scope and value, not a running count, scrubbed names are listed in their scrubbed order, and files sent to a model that sees them scrubbed go in random order;
- read one scope through another's view: the vault keeps one table per scope, bound to the scope's directories (a policy that reuses the name for other data starts afresh, and the old placeholders turn back into nothing), so a scope's placeholders and what it replaces come from its own files alone (two scopes holding the same email give it two placeholders);
- learn anything from naming paths in a scope it sees nothing of: such a path is labelled from its name alone (links are followed one hop at a time and only up to a private directory, never inside it, however the path is spelt or linked), and the door looks for its files only inside the job, after the reply's time is fixed;
- follow links out of a scope inside a hidden job: a hidden input with a link anywhere between its scope's directory and the file fails its job (still charged), and links inside a staged scope are skipped by the node's runner, so a job pays for and reads only its own scope;
- name a file by inode (`/.vol/<dev>/<inode>`) or descriptor (`/dev/fd/N`): refused before any look;
- reach anything through a link: lego follows a link only while it walks a name from `/` to the first private directory, by `readlink`, one hop at a time. From there on (inside every private directory, and for the file it then opens) nothing is followed: every path is opened one component at a time from a folder held open, so a link in a private directory, or one the agent swaps into its workspace a moment after naming a file, gets the same refusal whatever it points at; a folder that holds private directories is never walked. The same holds on every machine: on another node, naming, listing, reading and the job's own reads all go through a small program that opens files the same way, and a hidden input on a third machine is never packed from this one's disk;
- probe the disk outside the private directories: a path that is in no private directory, not public and not in its workspace (labels go by exact spelling, so `HR` beside `hr` on a case-sensitive disk is not `hr`; a name holding a NUL is refused outright) is refused from its spelling alone, before anything looks at the disk, so a job's output file or anything else there gets the same answer whether it exists or not;
- measure a hidden job on this machine: `df` in the VM reads the free space of every volume, so a model that runs jobs on data hidden from the agent must be reached by ssh on another machine; lego refuses to start otherwise;
- time a job by what it was given: the cloud call runs in its own process, and staging a hidden folder runs as its own process group, both stopped at the job's bound;
- send names in `returns`: an enum member or int bound that the text-only rules would replace is refused for a model that sees the data scrubbed;
- get a shaped answer outside the outcomes it paid for: the hidden model's reply is parsed inside the job, before its bound, and anything odd (too long, too deep) is `UNKNOWN`;
- learn a hidden scope's folder layout from how long a listing takes: a scope the caller sees nothing of is counted once, before the agent starts, and a private directory inside another one or inside a public path is refused when the policy loads;
- watch a hidden job through its workspace: the agent's own files are read before the reply, a FIFO or other special file is refused, and no job touches the workspace afterwards;
- probe names: a caller that sees a scope scrubbed may name its files only by the placeholder names it was shown, and the level is checked before the file system is touched, so a guessed path gets the same refusal whether or not it exists. Files added after lego starts are counted, not named, until lego restarts and learns them;
- borrow another model's level: each cloud agent runs as the policy's model with its own `api`, and an agent with no entry sees nothing.

**Not covered:**

- **Scrubbed means de-identified, not secret.** A model that sees a scope scrubbed gets every number, date and sentence in it; only identifiers become placeholders. The vault finds them by pattern, by CSV column and by asking a local model, so an identifier none of the three catches (an unusual nickname, a name spelled out letter by letter) crosses as written. A dictionary word is never cut up (a part named Tran inside `transfer` stays put), unless it is itself a known name in any case or glued into a camel-case word (`Baker`, `bAkEr`, `DrBaker`, `QuellWater`), and a word that does hold a known name becomes its placeholders alone, with no letters left around them to spell the name. A known name is also caught in its usual plain spellings (Lukasz or Soeren for Łukasz or Søren, O Brien for O'Brien), but not in a transliteration from another script. Each view is built from its own scope's identifiers only, learned from that scope's files before the agent starts and never from text passing through (so the hr view says nothing about keys): a key or name from another scope written into an hr file is hr data there, and is replaced only if hr's own detection finds it. A file that is not text (a PDF, a spreadsheet) is never sent scrubbed. Text in a file that only looks like a placeholder (`[EMP_10234]`) is scrubbed like any other text. A file whose identifiers are machine-encoded (HTML entities, `%XX`, `\uXXXX`, quoted-printable, MIME header words, RTF and `\x` escapes, short base64) is refused rather than half-scrubbed; other reversible encodings of a known name (hex, rot13) are not caught. The proof's bound is about what lies beyond the scrubbed view, so it says nothing about how good the scrubber is. The canary runs below measure that.
- **Local agent in front: it is trusted, the cloud is not.** With `lego omp`, what crosses to the cloud is what your local agent chooses to write, scrubbed. Every known identifier is replaced, but the agent decides what to say about the data. The cloud's replies are untrusted text that the local agent reads, so a cloud reply could try to talk it into sending more (prompt injection). The level still holds: nothing from a scope the cloud sees as `nothing` crosses once the agent has read it, because its context then carries that scope. The cloud's reply could also ask your agent to send back a list of guessed names, to see which ones the scrubber replaces. So what your agent writes is scrubbed by rules that look at the text alone, never at the vault: emails, phones and the other patterns, dates, every run of four or more digits, every capitalised word that is not an everyday word, and every word not in the dictionary (an inflection counts only by English's rules: `catches`, not `hayes`) each get a placeholder of their own, never the one the cloud saw in the files. A relayed list of guesses therefore comes out the same whatever the vault holds. Capitalised words count as names except a common sentence opener ("The", "Write", "Please") that does not follow a title, an initial or a short capitalised abbreviation ("Prof. Good", "J. Read", "Det. Read" are names); mixed letters and digits (`e10234`) count as ids; a bracketed tag your agent did not get from lego is treated as text. The price: the cloud also sees placeholders for acronyms and short technical words it would have understood (a fixed list of long ones, such as `offload` and `workspace`, is kept; short ones like `repo` or `auth` are surnames somewhere). First names from a fixed list (`/usr/share/dict/propernames`) are replaced in any case. What still crosses as written: a surname that is an everyday word or reads as the plural of one, in lower case ("brown", "jenkins", or a file your agent calls `amber_baker.md`), even when the cloud asks your agent to quote one of its placeholders that way, including a whole private file your agent copies into its workspace (lego no longer tells such copies apart, since doing so told the cloud which of its guesses matched a file). These rules stop accidents; they cannot stop your own agent from spelling a name out on purpose, since it sees the scope raw. Scopes the cloud sees as `nothing` are not scrubbed in it at all, since your agent's context keeps their contents out.

- **Bits are bits.** 16 bits about hr is enough for one exact salary if the agent asks `int[100000, 150000]`. The allowance bounds how much is learnt, not which facts, so size each scope's allowance with that in mind.
- **The host is trusted.** All bytes between nodes pass through the Mac, and policy requires the Mac to be trusted with every scope. A job on data hidden from the agent is staged in one `tar` pipe and walked on the model's machine, so the door does the same work whatever it holds; the Mac's other cores still copy those bytes, and that load is not hidden.
- **VM scope.** The VM is Apple's `container` (Virtualization.framework). It has outbound network; the agent's only way to private data is the `lego` tool, but anything already in its workspace or context can be sent anywhere. Its boundary is tested by probes, not proved.
- **Local model quality.** If the local model miscounts, the answer is wrong but still shaped.
- **What the proof assumes.** The Lean proof covers any size, but it is a proof about a model of the door. It assumes a job really stops at its time limit, and that the node's power draw is not observed. An agent on this Mac could read this Mac's network byte counters, so lego keeps a hidden job's traffic independent of what it was asked: any scope the agent does not see raw crosses to a node whole, once, whatever path was named (a job's own request pads each file name to one size), a job's output it may not see never leaves the node that made it, and a hidden job (shaped or receipt) replies over ssh with only the parsed answer, padded to one size and sent at the end of its time slot, whether the model finished early, late or failed; nothing on this machine looks inside a hidden scope to decide whether a job runs. A hidden job therefore holds its node for its whole slot, and the policy may not give one model server two names (the queue that keeps jobs from timing each other is per name). A scope on a third machine is never packed from this one's disk. Any process of this user can read every other one's command line and environment (sysctl kern.procargs2, which the macOS sandbox does not stop even when every sysctl read is denied), so no path goes on a command line: a node gets its path on stdin, padded to one size. A scope on another machine that the agent sees scrubbed or not at all is read once, when lego starts; its listing and scrubbed reads come from that copy, so nothing about it crosses the network at request time, and files added there later are not seen until a restart. What still shows is such a scope's total size, at its first job. The door's own log, on this machine's disk, which df reads, is written in records of one size (4 KiB), and the listing tells new files from their names alone, never reading them. `twin.py` checks the door's decisions against the model on random inputs, not on all of them. See [`verify/README.md`](verify/README.md#limits).
- **Latency.** A public job sent right after private work waits out that work's full time limit (`job_timeout`), however quickly it really finished.
