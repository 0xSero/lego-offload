# Lego core: brick laws, checked exhaustively.
#   L1 labels form a join semilattice: union is commutative, associative, idempotent,
#      monotone, and an upper bound (all 8^3 scope-mask triples)
#   L2 trust is all-or-nothing and composes: a run on node n accepts inputs x, y exactly
#      when x|y fits n, and anything that fits n fits every node trusting a superset
#   L3 shape cost: for every answer-set size n in 1..65536, bits(n) = ceil(log2(n+1)) is
#      the smallest b with 2^b >= n+1; sizes above 65535 cost more than 16 bits
#   L4 charge: for ledgers 0..31 and costs 1..16 the balance never goes negative and a
#      refusal happens exactly when the balance is short
# Each check returns (cases, failures). verify/lemmas_ref.py recomputes the bits table
# with core.bits and compares the checksum.

def subset(x, y):
  return (x & y) == x

def l1(x, y, z):
  comm = ((x | y) == (y | x))
  assoc = (((x | y) | z) == (x | (y | z)))
  idem = ((x | x) == x)
  upper = subset(x, x | y) & subset(y, x | y)
  mono = (subset(x, y) == 0) | subset(x | z, y | z)
  return comm & assoc & idem & upper & mono

def l2(x, y, t, u):
  run_ok = subset(x | y, t)
  both = subset(x, t) & subset(y, t)
  widen = (subset(x, t) & subset(t, u)) == 0
  return (run_ok == both) & (widen | subset(x, u))

def sweep1(i, n):
  if n == 1:
    return (1, 1 - l1(i & 7, (i >> 3) & 7, (i >> 6) & 7))
  else:
    h = n / 2
    (c1, f1) = sweep1(i, h)
    (c2, f2) = sweep1(i + h, n - h)
    return (c1 + c2, f1 + f2)

def sweep2(i, n):
  if n == 1:
    return (1, 1 - l2(i & 7, (i >> 3) & 7, (i >> 6) & 7, (i >> 9) & 7))
  else:
    h = n / 2
    (c1, f1) = sweep2(i, h)
    (c2, f2) = sweep2(i + h, n - h)
    return (c1 + c2, f1 + f2)

def pow2(b):
  if b == 0:
    return 1
  else:
    return 2 * pow2(b - 1)

# smallest b with 2^b >= m, by counting
def clog2(m, b):
  if pow2(b) < m:
    return clog2(m, b + 1)
  else:
    return b

def l3(n):
  b = clog2(n + 1, 0)
  tight = (pow2(b) < (n + 1)) == 0
  minimal = (b == 0) | (pow2(b - 1) < (n + 1))
  cap = (b > 16) == (n > 65535)
  return (tight & minimal & cap, b)

# (cases, failures, sum of bits) over sizes i..i+n-1
def sweep3(i, n):
  if n == 1:
    (ok, b) = l3(i)
    return (1, 1 - ok, b)
  else:
    h = n / 2
    (c1, f1, s1) = sweep3(i, h)
    (c2, f2, s2) = sweep3(i + h, n - h)
    return (c1 + c2, f1 + f2, s1 + s2)

def l4(bal, k):
  ok = (bal < k) == 0
  after = bal - (k * ok)
  return (after < 32) & ((ok == 0) | (after == (bal - k))) & (ok == ((bal < k) == 0))

def sweep4(i, n):
  if n == 1:
    return (1, 1 - l4(i & 31, 1 + ((i >> 5) & 15)))
  else:
    h = n / 2
    (c1, f1) = sweep4(i, h)
    (c2, f2) = sweep4(i + h, n - h)
    return (c1 + c2, f1 + f2)

def main():
  return (sweep1(0, 512), sweep2(0, 4096), sweep3(1, 65536), sweep4(0, 512))
