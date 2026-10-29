# Lego core: Monte Carlo of a working day, 8192 sessions per cell, run in parallel.
#
# A session is 40 offloads drawn from a mix measured nowhere yet (an assumption, see
# README): 45% public work (full result), 35% private questions, 15% private documents
# (go to /out, the cloud gets a receipt), 5% questions about sealed files (receipt).
# Private question sizes: 30% bool (2 bits), 30% one of 4 (3 bits), 25% int 0..99
# (7 bits), 15% int 0..999 (10 bits). Job time: 2 + U[0,30) s, plus 60 s for 10%.
# Cells: budget B in {0, 8, 16, 32, 64} bits per session x reply time T in {30, 60, 120} s.
# A private question is answered if its bits fit the remaining budget and the job ends
# by T (else UNKNOWN, still charged). Every reply to a charged question comes at T.
# Per cell: (asked, answered, late, over budget, bits used, sum T - job time over answers)
# sim_ref.py reproduces every number with the same generator.

def rng(x):
  return ((x * 1664525) + 12345) & 16777215

def pick(r):
  return (r >> 8) % 100

def qbits(r):
  p = pick(r)
  if p < 30:
    return 2
  elif p < 60:
    return 3
  elif p < 85:
    return 7
  else:
    return 10

def jtime(r1, r2):
  base = 2 + ((r1 >> 8) % 30)
  if pick(r2) < 10:
    return base + 60
  else:
    return base

# one session: returns (asked, answered, late, over, bits, wait)
def session(x, n, left, t, acc):
  (asked, ans, late, over, bits, wait) = acc
  if n == 0:
    return acc
  else:
    r0 = rng(x)
    r1 = rng(r0)
    r2 = rng(r1)
    r3 = rng(r2)
    kind = pick(r0)
    if (kind < 45) | (kind >= 80):
      return session(r3, n - 1, left, t, acc)
    else:
      b = qbits(r1)
      if b > left:
        return session(r3, n - 1, left, t, (asked + 1, ans, late, over + 1, bits, wait))
      else:
        jt = jtime(r2, r3)
        if jt > t:
          return session(r3, n - 1, left - b, t, (asked + 1, ans, late + 1, over, bits + b, wait))
        else:
          return session(r3, n - 1, left - b, t, (asked + 1, ans + 1, late, over, bits + b, wait + (t - jt)))

def add(a, b):
  (a1, a2, a3, a4, a5, a6) = a
  (b1, b2, b3, b4, b5, b6) = b
  return (a1 + b1, a2 + b2, a3 + b3, a4 + b4, a5 + b5, a6 + b6)

def sessions(seed, i, n, budget, t):
  if n == 1:
    return session(rng(seed + (i * 7919)), 40, budget, t, (0, 0, 0, 0, 0, 0))
  else:
    h = n / 2
    return add(sessions(seed, i, h, budget, t), sessions(seed, i + h, n - h, budget, t))

def cells(bs, t):
  match bs:
    case List/Nil:
      return []
    case List/Cons:
      return List/Cons((bs.head, t, sessions(4242, 0, 8192, bs.head, t)), cells(bs.tail, t))

def main():
  bs = [0, 8, 16, 32, 64]
  return (cells(bs, 30), cells(bs, 60), cells(bs, 120))
