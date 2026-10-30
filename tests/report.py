"""Collect results/*.json into results/REPORT.md (printed to stdout). Missing parts are skipped."""
import json
import subprocess
from pathlib import Path

R = Path(__file__).resolve().parent.parent / "results"


def load(name):
    p = R / name
    return (json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()) if p.exists() else None


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] +
                     ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


out = ["# Lego offload: results\n"]
root = R.parent
src = ["core.py", "door.py", "transport.py", "route.py", "room.py", "agents.py", "lego.py", "room/room.sb"]
loc = {f: sum(1 for _ in open(root / f)) for f in src}
out += [f"Implementation: {sum(loc.values())} lines ({', '.join(f'{f} {n}' for f, n in loc.items())}).",
        "commit: " + subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True).stdout.strip() + "\n"]

if ag := load("agents.txt"):
    out += ["## Agent adapters (no model): credentials and wiring of every room\n", "```", ag.strip(), "```\n"]

if lean := load("lean.txt"):
    axioms = [l.strip() for l in lean.splitlines() if "depends on axioms" in l]
    out += ["## Unbounded proof (Lean 4, `verify/Lego.lean`)\n",
            "Any policy, any number of nodes, scopes and calls, any adaptive strategy; reply times, the node "
            "queue and the prompt cache in the model. Each theorem and the axioms its proof uses:\n",
            "```", *axioms, "```", next((l for l in lean.splitlines() if l.startswith("LEAN")), ""), ""]
if twin := load("twin.txt"):
    out += ["## Lean model vs the real door\n", "```", twin.strip().splitlines()[-1], "```", ""]
for when in ("before", "after"):
    if c := load(f"contention_{when}.json"):
        if when == "before":
            out += ["## Contention on one model node (gpu-box)\n",
                    "A free private job runs long or short depending on a salary; 3 s later a public job is sent "
                    "to the same node. Public reply times by side:\n"]
        rows = [[s, " ".join(f"{x:.2f}" for x in sorted(r["public_seconds"] for r in c["rows"] if r["salary"] == s)),
                 c["mean_full_seconds"][s]] for s in ("above", "below")]
        out += [f"**{'before' if when == 'before' else 'with'} the queue** ({c['date']}, {c['trials_per_side']} trials per side)\n",
                table(["salary", "public reply (s), sorted", "mean"], rows), ""]

if s := load("sim.json"):
    out += ["## Simulation (8192 sessions per cell, 40 offloads each)\n",
            table(["bits/session", "reply time", "answered", "late (UNKNOWN)", "over budget", "mean extra wait"],
                  [[x["budget"], f"{x['T']}s", f"{x['answered_pct']:.1f}%", f"{100 * x['late'] / x['asked']:.1f}%",
                    f"{100 * x['over_budget'] / x['asked']:.1f}%",
                    "-" if x["mean_extra_wait_s"] is None else f"{x['mean_extra_wait_s']:.1f}s"] for x in s]), ""]

if e := load("e2e.json"):
    model = [c for c in e["cases"] if c["agent"] != "-"]
    names = list(dict.fromkeys(c["case"] for c in model))
    cell = lambda c: f"[{'PASS' if c['pass'] else 'FAIL'}](traces/{c['agent']}/{c['case']}.md)"
    out += [f"## Real models ({e['date']})\n", f"Local model: {e['local_model']}. {e['canaries']} canaries scanned in "
            f"every byte that crossed the route, both directions, decompressed. **{e['passed']}/{len(e['cases'])} passed.**\n",
            table(["agent"] + names, [[a] + [next((cell(c) for c in model if c["agent"] == a and c["case"] == n), "-")
                                         for n in names] for a in e["agents"]]), "",
            "A canary is **leaked** if it reaches the cloud before the cloud model wrote it. One the model wrote "
            "first, worked out from shaped answers it paid for, is listed as **derived**.\n",
            "**agent ended**: `ok`, `error` (the agent exited non-zero, for example because its cloud model blocked "
            "the prompt; see the trace), or `time limit` (still trying at 900 s).\n",
            table(["agent", "case", "s", "agent ended", "offloads", "replies", "refused", "bits charged", "bytes via route",
                   "leaked", "derived"],
                  [[c["agent"], c["case"], c["seconds"],
                    "time limit" if c["seconds"] >= 899 else "error" if c["agent_error"] else "ok", c["offloads"],
                    " ".join(f"{r}×{c['rows'].count(r)}" for r in dict.fromkeys(c["rows"])) or "-", c["refused"],
                    " ".join(f"{k}:{v}" for k, v in c["bits_charged"].items() if v) or "0", c["cloud_bytes"],
                    ", ".join(c["leaked"]) or "none", ", ".join(c.get("derived", [])) or "-"] for c in model]), ""]
    for c in e["cases"]:
        if "tries" in c:
            out += [f"### scripted attacker on the door ({len(c['tries'])} tricks, reply times {c['shaped_reply_s']} s "
                    f"for shaped answers, at most {c['max_other_reply_s']:.1f} s otherwise)\n",
                    table(["trick", "expected", "got", "s"],
                          [[t["try"], t["want"], t["got"], t["seconds"]] for t in c["tries"]]), ""]
    out += ["### What each agent said at the end of each case\n"]
    out += [f"**{c['agent']} / {c['case']}**: " + " ".join(c["reply"].split())[-300:] + "\n" for c in model]

print("\n".join(out))
