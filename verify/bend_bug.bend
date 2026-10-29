# Bend 0.2.38 miscompile: inside an if-branch that assigns two variables, a
# self-referential assignment (`la = la + k`, `la = f(la)`) keeps the old value.
# Expected ((2, 1), (2, ((2, 1), (2, 1)))). Observed on run-rs and run-c:
# ((0, 1), (2, ((0, 1), (0, 1)))). One variable per branch is fine.
def min2(a, b):
  if a < b:
    return a
  else:
    return b

def id(a):
  return a

def two_vars_call(c, k):
  la = 0
  w = 0
  if c:
    la = min2(la + k, 3)
    w = 1
  else:
    la = la
    w = w
  return (la, w)

def one_var_call(c, k):
  la = 0
  if c:
    la = min2(la + k, 3)
  else:
    la = la
  return la

def two_vars_op(c, k):
  la = 0
  w = 0
  if c:
    la = la + k
    w = 1
  else:
    la = la
    w = w
  return (la, w)

def two_vars_id(c, k):
  la = 0
  w = 0
  if c:
    la = id(la + k)
    w = 1
  else:
    la = la
    w = w
  return (la, w)

def main():
  return (two_vars_call(1, 2), one_var_call(1, 2), two_vars_op(1, 2), two_vars_id(1, 2))
