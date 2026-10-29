# Lego core: exhaustive model check over every trust topology.
#
# Independent of core.py. verify/model_ref.py explores the same world by calling the
# real core.py bricks, and verify/check.py asserts both agree on every number.
#
# World
#   job nodes 0,1,2 with trust masks from T (3 bits each); the cloud trusts nothing
#   scopes: a=1 b=2 sealed=4; allowance (bits) for a and b from al (2 bits each)
#   sources: 0 README (public, on node 0), 1 hr (a), 2 acme (b), 3 .env (unlisted ->
#            sealed); each private source lives on the first node trusted with it,
#            or nowhere if no node is
#   two result slots; the cloud calls offload(inputs, node, returns) forever:
#   a in [0,567): inputs = a/9+1 (any non-empty subset of the 6 items), node = (a/3)%3,
#   returns = a%3 (0 none, 1 one-option answer = 1 bit, 2 bool = 2 bits)
# State, 24 bits
#   r0 0-7 / r1 8-15: node 0-1 (3 = empty), label 2-4, taint 5-7   (taint = truth)
#   ledger a 16-17, ledger b 18-19 (bits left), leak a 20-21, leak b 22-23 (bits learnt)
# Events per transition: 1 contain, 2 noninterference, 4 budget (violations, terminal),
#   8 useful (the cloud got an answer derived from private data, with no violation)
# Mutants (cfg bits, 63 = all bricks): 1 LABEL 2 MOVE 4 RUN 8 SHAPE 16 BUDGET 32 CLOCK
#
# Bend 0.2.38 miscompiles `x = f(x)` inside an if-branch that assigns two variables
# (bend_bug.bend), so this file never reassigns a name inside a branch.

def none_s():
  return 16777215

def on(cfg, bit):
  return (cfg & bit) != 0

def getf(s, off, mask):
  return (s >> off) & mask

def setf(s, off, mask, v):
  return (s - (((s >> off) & mask) << off)) + (v << off)

def min2(a, b):
  if a < b:
    return a
  else:
    return b

def subset(x, y):
  return (x & y) == x

def trust(t, n):
  return (t >> (n * 3)) & 7

def home(t, scope):
  if subset(scope, trust(t, 0)):
    return 0
  elif subset(scope, trust(t, 1)):
    return 1
  elif subset(scope, trust(t, 2)):
    return 2
  else:
    return 3

# brick 1, label (fail-closed; the mutant labels the unlisted .env public)
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

def node_of(t, s, i):
  switch i:
    case 0:
      return 0
    case 1:
      return home(t, 1)
    case 2:
      return home(t, 2)
    case 3:
      return home(t, 4)
    case 4:
      return getf(s, 0, 3)
    case _:
      return getf(s, 8, 3)

# brick 2, move: every input must be allowed onto node n before anything moves.
# Returns (ok, label, taint, contain) where contain = a move put truth on an untrusted node.
def gather(cfg, t, s, m, n, i, ok, lab, tnt, cf):
  if i == 6:
    return (ok, lab, tnt, cf)
  elif ((m >> i) & 1) == 0:
    return gather(cfg, t, s, m, n, i + 1, ok, lab, tnt, cf)
  else:
    at = node_of(t, s, i)
    l = lab_of(cfg, s, i)
    x = tnt_of(s, i)
    if at == 3:
      return (0, 0, 0, 0)
    elif at == n:
      return gather(cfg, t, s, m, n, i + 1, ok, lab | l, tnt | x, cf)
    else:
      mv = (on(cfg, 2) == 0) | subset(l, trust(t, n))
      bad = subset(x, trust(t, n)) == 0
      return gather(cfg, t, s, m, n, i + 1, ok & mv, lab | l, tnt | x, cf | bad)

def store(s, n, l, x):
  v = n + (l << 2) + (x << 5)
  if getf(s, 0, 3) == 3:
    return setf(s, 0, 255, v)
  else:
    return setf(s, 8, 255, v)

# bricks 4-6: the reply row is chosen from labels and the ledger only
def budget_ok(cfg, s, k, l):
  if on(cfg, 16):
    oka = ((l & 1) == 0) | ((getf(s, 16, 3) < k) == 0)
    okb = ((l & 2) == 0) | ((getf(s, 18, 3) < k) == 0)
    return oka & okb & ((l & 4) == 0)
  else:
    return 1

def debit(cfg, s, k, l):
  if on(cfg, 16):
    sa = setf(s, 16, 3, getf(s, 16, 3) - (k * (l & 1)))
    return setf(sa, 18, 3, getf(sa, 18, 3) - (k * ((l >> 1) & 1)))
  else:
    return s

def shaped(cfg, al, s, k, l, x):
  if budget_ok(cfg, s, k, l):
    s1 = debit(cfg, s, k, l)
    lka = min2(getf(s1, 20, 3) + (k * (x & 1)), 3)
    lkb = min2(getf(s1, 22, 3) + (k * ((x >> 1) & 1)), 3)
    s2 = setf(setf(s1, 20, 3, lka), 22, 3, lkb)
    over = (lka > (al & 3)) | (lkb > ((al >> 2) & 3)) | ((x & 4) != 0)
    raw = ((on(cfg, 8) == 0) | (on(cfg, 32) == 0)) & (x != 0)
    got = (x & 3) != 0
    return (s2, (2 * raw) + (4 * over) + (8 * got))
  else:
    return (s, 0)

def release(cfg, al, s, ret, l, x):
  if l == 0:
    return (s, 2 * (x != 0))
  elif ret == 0:
    return (s, 0)
  else:
    return shaped(cfg, al, s, ret, l, x)

# brick 3, run: the output keeps every input scope and must fit the node
def step(cfg, t, al, s, a):
  m = (a / 9) + 1
  n = (a / 3) % 3
  ret = a % 3
  (ok, lab, tnt, cf) = gather(cfg, t, s, m, n, 0, 1, 0, 0, 0)
  if ok == 0:
    return (none_s(), 0)
  elif on(cfg, 4) == 0:
    (s2, ev) = release(cfg, al, store(s, n, 0, tnt), ret, 0, tnt)
    return (s2, ev | cf)
  elif subset(lab, trust(t, n)):
    (s3, ev3) = release(cfg, al, store(s, n, lab, tnt), ret, lab, tnt)
    return (s3, ev3 | cf)
  elif cf:
    return (s, 1)
  else:
    return (none_s(), 0)

# ---------- deduplicated BFS to a fixpoint, shortest witness per event ----------

def pick(e, hit, d, p, a):
  (d0, p0, a0) = e
  if hit & (d < d0):
    return (d, p, a)
  else:
    return (d0, p0, a0)

def note(evs, ev, d, p, a):
  (e0, e1, e2, e3) = evs
  return (pick(e0, ev & 1, d, p, a), pick(e1, (ev >> 1) & 1, d, p, a), pick(e2, (ev >> 2) & 1, d, p, a), pick(e3, ((ev >> 3) & 1) & ((ev & 7) == 0), d, p, a))

def succ(cfg, t, al, s, a, d, seen, next, evs):
  if a == 567:
    return (seen, next, evs)
  else:
    (n, ev) = step(cfg, t, al, s, a)
    evs2 = note(evs, ev, d, s, a)
    if (ev & 7) != 0:
      return succ(cfg, t, al, s, a + 1, d, seen, next, evs2)
    elif (n == s) | (n == none_s()):
      return succ(cfg, t, al, s, a + 1, d, seen, next, evs2)
    else:
      (has, seen1) = Map/contains(seen, n)
      if has == 1:
        return succ(cfg, t, al, s, a + 1, d, seen1, next, evs2)
      else:
        seen2 = Map/set(seen1, n, (s, a, d))
        return succ(cfg, t, al, s, a + 1, d, seen2, List/Cons(n, next), evs2)

def level(cfg, t, al, frontier, d, seen, next, evs):
  match frontier:
    case List/Nil:
      return (seen, next, evs)
    case List/Cons:
      (seen1, next1, evs1) = succ(cfg, t, al, frontier.head, 0, d, seen, next, evs)
      return level(cfg, t, al, frontier.tail, d, seen1, next1, evs1)

def bfs(cfg, t, al, frontier, seen, count, d, evs):
  (len, frontier1) = List/length(frontier)
  if len == 0:
    return (seen, count, d - 1, evs)
  else:
    (seen1, next, evs1) = level(cfg, t, al, frontier1, d + 1, seen, List/Nil, evs)
    (k, next1) = List/length(next)
    return bfs(cfg, t, al, next1, seen1, count + k, d + 1, evs1)

def trace(seen, s, init, acc):
  if s == init:
    return (acc, seen)
  else:
    (v, seen1) = Map/get(seen, s)
    (q, a, dd) = v
    return trace(seen1, q, init, List/Cons(a, acc))

def witness(seen, e, init):
  (d, p, a) = e
  if d == 99:
    return ((99, []), seen)
  else:
    (tr, seen1) = trace(seen, p, init, [a])
    return ((d, tr), seen1)

def init_state(al):
  return 3 + (3 << 8) + ((al & 3) << 16) + (((al >> 2) & 3) << 18)

def explore(cfg, t, al):
  s0 = init_state(al)
  nil3 = (99, 0, 0)
  seen = Map/set(Map/Leaf, s0, (s0, 999, 0))
  return (s0, bfs(cfg, t, al, [s0], seen, 1, 0, (nil3, nil3, nil3, nil3)))

# states, diameter and the depth of each event's shortest witness (99 = never)
def check(cfg, t, al):
  (s0, r) = explore(cfg, t, al)
  (seen, n, diam, evs) = r
  (e0, e1, e2, e3) = evs
  (d0, p0, a0) = e0
  (d1, p1, a1) = e1
  (d2, p2, a2) = e2
  (d3, p3, a3) = e3
  return (cfg, t, al, n, diam, [d0, d1, d2, d3])

# the same, with the action trace of each witness
def check_tr(cfg, t, al):
  (s0, r) = explore(cfg, t, al)
  (seen, n, diam, evs) = r
  (e0, e1, e2, e3) = evs
  (w0, seen1) = witness(seen, e0, s0)
  (w1, seen2) = witness(seen1, e1, s0)
  (w2, seen3) = witness(seen2, e2, s0)
  (w3, seen4) = witness(seen3, e3, s0)
  return (cfg, t, al, n, diam, [w0, w1, w2, w3])

# ---------- the runs ----------
# standard topology: node 0 "pop" trusts {a}, node 1 "ally" trusts {b}, node 2 "mac" all

def std_t():
  return 1 + (2 << 3) + (7 << 6)

def std_al():
  return 2 + (1 << 2)

def topologies(t, n):
  if n == 0:
    return []
  else:
    return List/Cons(check(63, t, std_al()), topologies(t + 1, n - 1))

def allowances(al, n):
  if n == 0:
    return []
  elif ((al & 3) == 3) | (((al >> 2) & 3) == 3):
    return allowances(al + 1, n - 1)
  else:
    return List/Cons(check(63, std_t(), al), allowances(al + 1, n - 1))

def mutants(vs, t):
  match vs:
    case List/Nil:
      return []
    case List/Cons:
      return List/Cons(check_tr(vs.head, t, std_al()), mutants(vs.tail, t))

# each mutant over every topology: how many of the 512 topologies expose it
def violated(cfg, t, al):
  (s0, r) = explore(cfg, t, al)
  (seen, n, diam, evs) = r
  (e0, e1, e2, e3) = evs
  (d0, p0, a0) = e0
  (d1, p1, a1) = e1
  (d2, p2, a2) = e2
  return (d0 != 99) | (d1 != 99) | (d2 != 99)

def catch(cfg, t, n):
  if n == 0:
    return 0
  else:
    return violated(cfg, t, std_al()) + catch(cfg, t + 1, n - 1)

def catches(vs):
  match vs:
    case List/Nil:
      return []
    case List/Cons:
      return List/Cons((vs.head, catch(vs.head, 0, 512)), catches(vs.tail))

def main():
  vs = [63, 62, 61, 59, 55, 47, 31]
  return (mutants(vs, std_t()), allowances(0, 16), topologies(0, 512), catches([62, 61, 59, 55, 47, 31]))
