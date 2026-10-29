"""Compare qif.bend with the core-driven qif_ref.py and summarise.
usage: python3 qif_check.py runs/qif_raw.txt runs/qif_ref.json OUT.json
"""
import ast
import json
import sys

NAMES = {63: "all bricks", 62: "label fails open", 61: "no move check", 59: "run drops labels",
         55: "no shape", 47: "no budget", 31: "no clock"}


def flat(t):
    out = []
    while isinstance(t, tuple) and len(t) == 2 and isinstance(t[1], tuple):
        out.append(t[0])
        t = t[1]
    out.extend(t if isinstance(t, tuple) else [t])
    return out


line = next(l for l in open(sys.argv[1]) if l.startswith("Result: "))[8:]
bend = {flat(r)[0]: flat(r)[1:] for r in ast.literal_eval(line)}
ref = {r["cfg"]: r["result"] for r in json.load(open(sys.argv[2]))}
rows, ok = [], True
for cfg, name in NAMES.items():
    b, r = bend[cfg], ref[cfg]
    same = list(b) == list(r)
    ok &= same
    word = lambda d: "holds" if d == 99 else f"FAIL@{d}"
    rows.append({"variant": name, "traces": r[0], "A_hr": word(r[1]), "B_acme": word(r[2]), "S_sealed": word(r[3]),
                 "T_total": word(r[4]), "max_hr_classes": r[5], "bend_matches": same})
json.dump(rows, open(sys.argv[3], "w"), indent=1)
print(f"{'variant':18} {'traces':>8}  {'hr<=2^ca':>9} {'acme<=2^cb':>10} {'sealed=1':>9} {'total':>8}  hr-classes  match")
for x in rows:
    print(f"{x['variant']:18} {x['traces']:>8}  {x['A_hr']:>9} {x['B_acme']:>10} {x['S_sealed']:>9} {x['T_total']:>8}  "
          f"{x['max_hr_classes']:>10}  {x['bend_matches']}")
print("QIF AGREES" if ok else "QIF MISMATCH")
sys.exit(0 if ok else 1)
