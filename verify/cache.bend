# Shared node state: the local model's prompt cache, checked by self-composition.
#
# Independent of core.py. verify/cache_ref.py explores the same world with the real
# core.salt and checks that both agree on every number.
#
# World
#   one node with a prefix cache; a secret hr value w in 0..7 (8 worlds run side by side)
#   the cloud picks any sequence of up to 4 jobs, a in [0,18):
#     a < 2   a private job over hr with task prefix a (a receipt: costs nothing, replies
#             at once); its prompt "task a + hr file" is cached under salt(hr)
#     a >= 2  a public job (a full reply, timed by the cloud) whose task text is
#             "task t + guess g", t = (a-2)/8, g = (a-2)%8; it hits the cache when an
#             entry with its salt, task and value exists, and the hit makes it faster
# Observation per world: the hit bit of every public job, appended to a signature.
# Noninterference: after every trace all 8 worlds have the same signature (1 class).
# cfg 1 = salted by scope (core.salt); cfg 0 = the mutant with one shared cache.
#
# Bend 0.2.38 miscompiles `x = f(x)` inside an if-branch that assigns two variables
# (bend_bug.bend), so this file never reassigns a name inside a branch.

def key(cfg, sc, t, v):
  if cfg == 1:
    return (sc * 64) + (t * 8) + v
  else:
    return (t * 8) + v

def has(c, k):
  match c:
    case List/Nil:
      return 0
    case List/Cons:
      if c.head == k:
        return 1
      else:
        return has(c.tail, k)

# one job in world w: (cache after, hit bit)
def act(cfg, w, c, a):
  if a < 2:
    return (List/Cons(key(cfg, 1, a, w), c), 0)
  else:
    k = key(cfg, 0, (a - 2) / 8, (a - 2) % 8)
    return (List/Cons(k, c), has(c, k))

def apply_all(cfg, w, ws, a):
  match ws:
    case List/Nil:
      return List/Nil
    case List/Cons:
      (c, sg) = ws.head
      (c2, h) = act(cfg, w, c, a)
      return List/Cons((c2, (sg * 2) + h), apply_all(cfg, w + 1, ws.tail, a))

def mem(x, l):
  match l:
    case List/Nil:
      return 0
    case List/Cons:
      if l.head == x:
        return 1
      else:
        return mem(x, l.tail)

# number of distinct signatures among the worlds
def classes(ws, seen):
  match ws:
    case List/Nil:
      (n, l) = List/length(seen)
      return n
    case List/Cons:
      (c, sg) = ws.head
      if mem(sg, seen):
        return classes(ws.tail, seen)
      else:
        return classes(ws.tail, List/Cons(sg, seen))

def min3(a, b, c):
  if (a < b) & (a < c):
    return a
  elif b < c:
    return b
  else:
    return c

def max3(a, b, c):
  if (a > b) & (a > c):
    return a
  elif b > c:
    return b
  else:
    return c

# (traces, violating traces, depth of the first violation (99 = never), most classes)
def loop(cfg, ws, d, maxd, a):
  if a == 18:
    return (0, 0, 99, 1)
  else:
    ws2 = apply_all(cfg, 0, ws, a)
    k = classes(ws2, List/Nil)
    (t1, v1, f1, m1) = dfs(cfg, ws2, d + 1, maxd)
    (t2, v2, f2, m2) = loop(cfg, ws, d, maxd, a + 1)
    bad = k > 1
    here = 99 - (bad * (98 - d))
    return (1 + t1 + t2, bad + v1 + v2, min3(here, f1, f2), max3(k, m1, m2))

def dfs(cfg, ws, d, maxd):
  if d == maxd:
    return (0, 0, 99, 1)
  else:
    return loop(cfg, ws, d, maxd, 0)

def worlds(w):
  if w == 8:
    return List/Nil
  else:
    return List/Cons((List/Nil, 0), worlds(w + 1))

def main():
  return [(1, dfs(1, worlds(0), 0, 4)), (0, dfs(0, worlds(0), 0, 4))]
