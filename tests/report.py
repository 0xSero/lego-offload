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

if lem := load("lemmas.txt"):
    out += ["## Brick laws (Bend, checked against core.py)\n", "```", lem.strip(), "```\n"]

if mc := load("model_check.json"):
    out += ["## Model check (Bend BFS, every reachable state, state counts, depths and every counterexample cross-checked against core.py)\n",
            table(["variant", "states", "containment", "noninterference", "budget"],
                  [[m["variant"], m["states"]] + ["holds" if m[e] == 99 else f"FAIL at step {m[e]}"
                                                   for e in ("contain", "noninterference", "budget")]
                   for m in mc["mutants"]]), "",
            f"All {mc['topologies']['count']} trust topologies: {mc['topologies']['violations']} violations over "
            f"{mc['topologies']['states_total']} states. Mutants caught, out of 512 topologies each: "
            + ", ".join(f"{k} {v}" for k, v in mc["mutant_catch_over_512_topologies"].items())
            + f". Bend agrees with core.py: {mc['bend_agrees_with_core']}.\n"]

if q := load("qif.json"):
    out += ["## Leak counting (16 secret worlds, every attack of up to 3 offloads)\n",
            "A trace holds when, per scope, the attacker's observations split the secret worlds into at most "
            "2^(bits charged) classes.\n",
            table(["variant", "traces", "hr", "acme", "sealed", "total", "max hr classes", "Bend = core"],
                  [[x["variant"], x["traces"], x["A_hr"], x["B_acme"], x["S_sealed"], x["T_total"],
                    x["max_hr_classes"], x["bend_matches"]] for x in q]), ""]

if c := load("cache.json"):
    out += ["## Shared prompt cache (8 secret worlds, every attack of up to 4 jobs)\n",
            "A trace holds when the reply times of public jobs are the same in every secret world.\n",
            table(["variant", "traces", "violating", "noninterference", "max classes", "Bend = core"],
                  [[x["variant"], x["traces"], x["violating"], x["first_violation"], x["max_classes"], x["bend_matches"]]
                   for x in c]), ""]

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
