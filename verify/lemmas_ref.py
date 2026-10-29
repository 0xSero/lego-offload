"""Recompute the lemmas.bend shape-cost table with the real core.bits and compare.
usage: python3 lemmas_ref.py runs/lemmas_raw.txt
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

line = next(l for l in open(sys.argv[1]) if l.startswith("Result: "))[8:]
(c1, f1), ((c2, f2), ((c3, (f3, s3)), (c4, f4))) = ast.literal_eval(line)
total = 0
for n in range(1, 65536):
    total += core.bits({"int": [0, n - 1]})
try:
    core.bits({"int": [0, 65535]})
    capped = False
except core.Refused:
    capped = True
total += 17                               # size 65536 needs 17 bits, which core must refuse
ok = (f1, f2, f3, f4) == (0, 0, 0, 0) and capped and total == s3 and (c1, c2, c3, c4) == (512, 4096, 65536, 512)
print(f"L1 lattice {c1} cases, {f1} failures | L2 trust {c2}, {f2} | L3 shape cost {c3}, {f3}, "
      f"Bend bits sum {s3} vs core.bits {total}, 17-bit size refused: {capped} | L4 charge {c4}, {f4}")
print("LEMMAS OK" if ok else "LEMMAS FAIL")
sys.exit(0 if ok else 1)
