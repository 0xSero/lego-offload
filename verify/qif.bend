# Lego core: quantitative noninterference by self-composition.
#
# model.bend tracks *which* secrets a value depends on. This file runs the system on
# 16 concrete secret worlds at once and checks what the cloud can actually tell apart:
#   xa in 0..3 (hr), xb in 0..1 (acme), xs in 0..1 (.env, sealed); world w = xa + 4*xb + 8*xs
# For every sequence of up to 3 offloads, the cloud's observations split the worlds into
# classes. With ca, cb = bits charged to a, b so far, the checks are (Koepf & Basin):
#   A: worlds differing only in xa       form at most 2^ca classes
#   B: worlds differing only in xb       form at most 2^cb classes
#   S: worlds differing only in xs       are never told apart
#   T: all 16 worlds                     form at most 2^(ca+cb) classes
# Topology: node 0 pop {a}, node 1 ally {b}, node 2 mac {a,b,sealed}; hr on pop, acme on
# ally, .env on mac, README (value 0) on pop. Allowance a = 2 bits, b = 1 bit.
# A job's output value is the sum of its inputs mod 4.
# Observation codes: 1+v full output, 5 receipt, 6+(v!=0) one-option answer,
# 8+min(v,2) bool answer, 11+v a reply whose time depends on v (no clock).
# Actions a in [0,189): inputs = pair(a/9) (one or two of the 6 items), node (a/3)%3, returns a%3.
# Public state (24 bits): r0 0-7, r1 8-15 (node, label, taint as in model.bend),
#   ledger a 16-17, ledger b 18-19, charged a 20-21, charged b 22-23.
# Per world: (v0, v1, h) = values in the two result slots and the observation history.
# Mutant cfg bits as in model.bend: 1 LABEL 2 MOVE 4 RUN 8 SHAPE 16 BUDGET 32 CLOCK.

def none_s():
  return 16777215

def on(cfg, bit):
  return (cfg & bit) != 0

def getf(s, off, mask):
  return (s >> off) & mask

def setf(s, off, mask, v):
  return (s - (((s >> off) & mask) << off)) + (v << off)

def max2(a, b):
  if a > b:
    return a
  else:
    return b

def subset(x, y):
  return (x & y) == x

def min2(a, b):
  if a < b:
    return a
  else:
    return b

def trust(n):
  switch n:
    case 0:
      return 1
    case 1:
      return 2
    case _:
      return 7

def pair(c):
  switch c:
    case 0:
      return 1
    case 1:
      return 2
    case 2:
      return 4
    case 3:
      return 8
    case 4:
      return 16
    case 5:
      return 32
    case 6:
      return 3
    case 7:
      return 5
    case 8:
      return 9
    case 9:
      return 17
    case 10:
      return 33
    case 11:
      return 6
    case 12:
      return 10
    case 13:
      return 18
    case 14:
      return 34
    case 15:
      return 12
    case 16:
      return 20
    case 17:
      return 36
    case 18:
      return 24
    case 19:
      return 40
    case _:
      return 48

def lab_of(cfg, s, i):
  switch i:
    case 0:
      return 0
    case 1:
      return 1
    case 2:
      return 2
    case 3:
      if on(cfg, 1):
        return 4
      else:
        return 0
    case 4:
      return getf(s, 2, 7)
    case _:
      return getf(s, 10, 7)

def tnt_of(s, i):
  switch i:
    case 0:
      return 0
    case 1:
      return 1
    case 2:
      return 2
    case 3:
      return 4
    case 4:
      return getf(s, 5, 7)
    case _:
      return getf(s, 13, 7)

def node_of(s, i):
  switch i:
    case 0:
      return 0
    case 1:
      return 0
    case 2:
      return 1
    case 3:
      return 2
    case 4:
      return getf(s, 0, 3)
    case _:
      return getf(s, 8, 3)

# (ok, label, taint) for moving the chosen inputs to node n
def gather(cfg, s, m, n, i, ok, lab, tnt):
  if i == 6:
    return (ok, lab, tnt)
  elif ((m >> i) & 1) == 0:
    return gather(cfg, s, m, n, i + 1, ok, lab, tnt)
  else:
    at = node_of(s, i)
    l = lab_of(cfg, s, i)
    if at == 3:
      return (0, 0, 0)
    elif at == n:
      return gather(cfg, s, m, n, i + 1, ok, lab | l, tnt | tnt_of(s, i))
    else:
      mv = (on(cfg, 2) == 0) | subset(l, trust(n))
      return gather(cfg, s, m, n, i + 1, ok & mv, lab | l, tnt | tnt_of(s, i))

def store(s, n, l, x):
  v = n + (l << 2) + (x << 5)
  if getf(s, 0, 3) == 3:
    return setf(s, 0, 255, v)
  else:
    return setf(s, 8, 255, v)

def budget_ok(cfg, s, k, l):
  if on(cfg, 16):
    oka = ((l & 1) == 0) | ((getf(s, 16, 3) < k) == 0)
    okb = ((l & 2) == 0) | ((getf(s, 18, 3) < k) == 0)
    return oka & okb & ((l & 4) == 0)
  else:
    return 1

# charge: debit the ledger and count the bits charged per scope
def charge(cfg, s, k, l):
  if on(cfg, 16):
    ka = k * (l & 1)
    kb = k * ((l >> 1) & 1)
    s1 = setf(setf(s, 16, 3, getf(s, 16, 3) - ka), 18, 3, getf(s, 18, 3) - kb)
    return setf(setf(s1, 20, 3, getf(s1, 20, 3) + ka), 22, 3, getf(s1, 22, 3) + kb)
  else:
    return s

# public step: (next public state or none, row) with row 0 full, 1 receipt, 2/3 shaped k=1/2
def pstep(cfg, s, a):
  m = pair(a / 9)
  n = (a / 3) % 3
  ret = a % 3
  (ok, lab0, tnt) = gather(cfg, s, m, n, 0, 1, 0, 0)
  lab = lab0 * on(cfg, 4)
  if ok == 0:
    return (none_s(), 0)
  elif on(cfg, 4) & (subset(lab, trust(n)) == 0):
    return (none_s(), 0)
  else:
    s1 = store(s, n, lab, tnt)
    if lab == 0:
      return (s1, 0)
    elif ret == 0:
      return (s1, 1)
    elif budget_ok(cfg, s1, ret, lab):
      return (charge(cfg, s1, ret, lab), 1 + ret)
    else:
      return (s1, 1)

def xval(w, i, v0, v1):
  switch i:
    case 0:
      return 0
    case 1:
      return w & 3
    case 2:
      return (w >> 2) & 1
    case 3:
      return (w >> 3) & 1
    case 4:
      return v0
    case _:
      return v1

def value(m, w, v0, v1, i, acc):
  if i == 6:
    return acc % 4
  else:
    return value(m, w, v0, v1, i + 1, acc + (((m >> i) & 1) * xval(w, i, v0, v1)))

def obs(cfg, row, v):
  if row == 0:
    return 1 + v
  elif row == 1:
    return 5
  elif on(cfg, 32) == 0:
    return 11 + v
  elif on(cfg, 8) == 0:
    return 1 + v
  elif row == 2:
    return 6 + (v != 0)
  else:
    return 8 + min2(v, 2)

# advance every world: store the new value in the slot the public state used
def wstep(cfg, ws, w, m, row, first):
  match ws:
    case List/Nil:
      return []
    case List/Cons:
      (v0, v1, h) = ws.head
      v = value(m, w, v0, v1, 0, 0)
      o = obs(cfg, row, v)
      if first:
        return List/Cons((v, v1, (h * 16) + o), wstep(cfg, ws.tail, w + 1, m, row, first))
      else:
        return List/Cons((v0, v, (h * 16) + o), wstep(cfg, ws.tail, w + 1, m, row, first))

def hist(ws, w):
  match ws:
    case List/Nil:
      return 0
    case List/Cons:
      if w == 0:
        (v0, v1, h) = ws.head
        return h
      else:
        return hist(ws.tail, w - 1)

# distinct histories among worlds base + step*j, j < n
def distinct(ws, base, step, n, j):
  if j == n:
    return 0
  else:
    return fresh(ws, base, step, j, 0) + distinct(ws, base, step, n, j + 1)

def fresh(ws, base, step, j, k):
  if k == j:
    return 1
  elif hist(ws, base + (step * j)) == hist(ws, base + (step * k)):
    return 0
  else:
    return fresh(ws, base, step, j, k + 1)

def max_over(ws, bases, step, n):
  match bases:
    case List/Nil:
      return 0
    case List/Cons:
      x = distinct(ws, bases.head, step, n, 0)
      y = max_over(ws, bases.tail, step, n)
      if x > y:
        return x
      else:
        return y

# violated checks at this node: bit 1 A, 2 B, 4 S, 8 T; plus classes for a
def judge(s, ws):
  ca = getf(s, 20, 3)
  cb = getf(s, 22, 3)
  na = max_over(ws, [0, 4, 8, 12], 1, 4)
  nb = max_over(ws, [0, 1, 2, 3, 8, 9, 10, 11], 4, 2)
  ns = max_over(ws, [0, 1, 2, 3, 4, 5, 6, 7], 8, 2)
  nt = distinct(ws, 0, 1, 16, 0)
  bad = (na > (1 << ca)) + (2 * (nb > (1 << cb))) + (4 * (ns > 1)) + (8 * (nt > (1 << (ca + cb))))
  return (bad, na)

def first_bad(bad, d, m):
  (fa, fb, fs, ft) = m
  return (pickd(fa, bad & 1, d), pickd(fb, (bad >> 1) & 1, d), pickd(fs, (bad >> 2) & 1, d), pickd(ft, (bad >> 3) & 1, d))

def pickd(f, hit, d):
  if hit & (d < f):
    return d
  else:
    return f

def merge(x, y):
  (n1, a1, b1, s1, t1, c1) = x
  (n2, a2, b2, s2, t2, c2) = y
  return (n1 + n2, min2(a1, a2), min2(b1, b2), min2(s1, s2), min2(t1, t2), max2(c1, c2))

# every action sequence up to `left` more steps; returns
# (trace nodes, first depth of A, B, S, T violations (99 = never), most a-classes seen)
def walk(cfg, s, ws, d, left):
  if left == 0:
    return (1, 99, 99, 99, 99, 0)
  else:
    return merge((1, 99, 99, 99, 99, 0), kids(cfg, s, ws, d, left, 0, 189))

def kids(cfg, s, ws, d, left, lo, n):
  if n == 1:
    (s1, row) = pstep(cfg, s, lo)
    if s1 == none_s():
      return (0, 99, 99, 99, 99, 0)
    else:
      ws1 = wstep(cfg, ws, 0, pair(lo / 9), row, getf(s, 0, 3) == 3)
      (bad, na) = judge(s1, ws1)
      if bad != 0:
        (fa, fb, fs, ft) = first_bad(bad, d + 1, (99, 99, 99, 99))
        return (1, fa, fb, fs, ft, na)
      else:
        return merge((0, 99, 99, 99, 99, na), walk(cfg, s1, ws1, d + 1, left - 1))
  else:
    h = n / 2
    return merge(kids(cfg, s, ws, d, left, lo, h), kids(cfg, s, ws, d, left, lo + h, n - h))

def worlds(w):
  if w == 16:
    return []
  else:
    return List/Cons((0, 0, 0), worlds(w + 1))

def run(cfg, depth):
  s0 = 3 + (3 << 8) + (2 << 16) + (1 << 18)
  return (cfg, walk(cfg, s0, worlds(0), 0, depth))

def main():
  return [run(63, 3), run(62, 3), run(61, 3), run(59, 3), run(55, 3), run(47, 3), run(31, 3)]
