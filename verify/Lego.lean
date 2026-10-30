/-
Layer 6: the door, proved for any number of nodes, scopes and steps.

Layers 2 and 3 check small worlds exhaustively in Bend. Here the same rules are stated over
any policy (nodes and scopes are Nat, trust is any relation), any catalog, and any adaptive
attacker: a strategy that picks each next call from everything it has seen so far. The local
model is the attacker's accomplice: it computes any function of its inputs, takes any time
that depends on them, and runs faster on a prompt-cache hit.

The door is modelled with the job queue it runs on each model node:
  * one job at a time per node, first in, first out;
  * a private job may start as soon as the node is free, so its timing depends on the job
    before it: it carries that job's scopes when it is submitted before that job's bound
    (the latest the job can end, its time limit after its latest start);
  * a public job carries no scope: it waits until the bound, when the node is surely idle;
  * the node's prompt cache is keyed by the job's scope set (core.salt, assumed collision-free).

Theorems, for every well-formed policy and every strategy:
  * `held_trusted`  : every byte the door places is on a node trusted with all its scopes;
  * `budget`        : per scope, bits spent + allowance left = allowance;
  * `reply_times`   : a shaped reply comes exactly `deadline` after the call;
  * `leak_bound`    : for any set S of scopes, the cloud's whole view is a function of a code
                      below 2 ^ (Σ_{s∈S} allowance s) when only the secrets in S vary; so it can
                      tell apart at most that many worlds (min-entropy leakage ≤ Σ allowance);
  * `sealed_hidden` : the sealed scope is never distinguished at all.
And each rule is load-bearing: `mutant_*` give, for the door with one rule removed, two worlds
that differ in one secret, with no bits charged, that the cloud tells apart.

`lean --run Lego.lean CASES` also runs the model on the call sequences in CASES so that
twin.py can compare every decision with the real core.py and door.py.
-/

namespace Lego

abbrev Scope := Nat
abbrev Node := Nat
abbrev Label := List Scope
abbrev World := Scope → Nat                -- one secret per scope

def agreeOn (L : Label) (w w' : World) : Prop := ∀ s ∈ L, w s = w' s
def dependsOn {α : Type} (g : World → α) (L : Label) : Prop := ∀ w w', agreeOn L w w' → g w = g w'

structure Policy where
  trust : Node → Scope → Bool
  model : Node → Bool
  allowance : Scope → Nat
  sealed : Scope
  deadline : Nat
  jobTimeout : Nat
  maxBits : Nat

/-- What `core.check_policy` and the door's start-up checks guarantee. -/
structure Policy.WF (P : Policy) : Prop where
  sealed_no_model : ∀ n, P.model n = true → P.trust n P.sealed = false

/-- Which rules the door applies. The real door is `{}`; each mutant switches one off. -/
structure Cfg where
  queue : Bool := true     -- taint private jobs by the job before them; public jobs wait
  salt : Bool := true      -- prompt cache keyed by scope set
  clock : Bool := true     -- shaped replies at the deadline; late answers are UNKNOWN
  charge : Bool := true    -- pay bits before a shaped answer
  shape : Bool := true     -- an answer outside the declared set becomes UNKNOWN
  run : Bool := true       -- an output keeps every scope of its job
  move : Bool := true      -- inputs go only to a node trusted with them

structure Item where
  node : Node
  label : Label
  val : World → Nat

structure Entry where
  key : Label
  prompt : World → List Nat

structure St where
  items : List Item                        -- the catalog, then every output (addressable refs)
  held : List (Node × Label)               -- every placement the door made: copies and outputs
  ledger : Scope → Nat
  spent : Scope → Nat
  bound : Node → Nat                       -- latest time the node's last job can end
  tl : Node → Label                        -- that job's scopes
  free : Node → World → Nat                -- when it really ends (depends on data)
  cache : Node → List Entry

structure Act where
  t : Nat                                  -- when the call reaches the door
  node : Node
  inputs : List Nat                        -- refs
  task : List Nat                          -- the task text
  f : List Nat → Nat                       -- what the local model writes
  dur : List Nat → Bool → Nat              -- how long it takes, given its prompt and a cache hit
  ret : Option (Nat × (Nat → Nat))         -- answer set {0..k-1} (k = UNKNOWN), and the model's pick

inductive Row | refused | receipt | full | shaped
  deriving DecidableEq, Repr

inductive Obs
  | refused
  | receipt (t : Nat)
  | full (v : Nat) (t : Nat)
  | shaped (a : Nat) (t : Nat)
  deriving DecidableEq, Repr

structure Meta where
  row : Row
  label : Label
  bits : Nat

/-- ⌈log2 (k+1)⌉, the bits that carry one of k answers or UNKNOWN: the least b with k < 2^b. -/
def bits (k : Nat) : Nat := go k 0
where go : Nat → Nat → Nat
  | 0, b => b
  | f + 1, b => if k < 2 ^ b then b else go f (b + 1)

def getAll (items : List Item) : List Nat → Option (List Item)
  | [] => some []
  | i :: is => match items[i]?, getAll items is with
    | some x, some xs => some (x :: xs)
    | _, _ => none

def sameSet (K O : Label) : Bool := K.all (O.contains ·) && O.all (K.contains ·)

def canCharge (P : Policy) (σ : St) (O : Label) (b : Nat) : Bool :=
  !(O.contains P.sealed) && O.all (fun s => decide (b ≤ σ.ledger s))

def shapeOk (P : Policy) : Option (Nat × (Nat → Nat)) → Bool
  | none => true
  | some (k, _) => decide (k ≠ 0) && decide (bits k ≤ P.maxBits)

def rowOf (c : Cfg) (P : Policy) (σ : St) (O : Label) : Option (Nat × (Nat → Nat)) → Row
  | none => if O = [] then .full else .receipt
  | some (k, _) => if O = [] then .full else
      if !c.charge || canCharge P σ O (bits k) then .shaped else .receipt

def clamp (k x : Nat) : Nat := if x < k then x else k

def w0 : World := fun _ => 0

namespace Step
variable (c : Cfg) (P : Policy) (σ : St) (a : Act) (ins : List Item)

def L : Label := ins.flatMap Item.label
def pub : Bool := decide (L ins = [])
/-- The scopes of the job before it, if this private job may be timed against that job. -/
def taint : Label := if c.queue && !pub ins && decide (a.t < σ.bound a.node) then σ.tl a.node else []
def O : Label := L ins ++ taint c σ a ins
def row : Row := rowOf c P σ (O c σ a ins) a.ret
def b : Nat := match a.ret with | none => 0 | some (k, _) => bits k
def timeout : Nat := if row c P σ a ins = .shaped then P.deadline else P.jobTimeout
def prompt (w : World) : List Nat := a.task ++ ins.map (fun i => i.val w)
def hit (w : World) : Bool :=
  (σ.cache a.node).any (fun e => (!c.salt || sameSet e.key (O c σ a ins)) && e.prompt w == prompt a ins w)
/-- A public job waits for the bound, when the node is surely idle; a private one starts when it is free. -/
def start (w : World) : Nat :=
  if c.queue && pub ins then max a.t (σ.bound a.node) else max a.t (σ.free a.node w)
def finish (w : World) : Nat :=
  start c σ a ins w + min (a.dur (prompt a ins w) (hit c σ a ins w)) (timeout c P σ a ins)
def out (w : World) : Nat := a.f (ins.map (fun i => i.val w))
def paid : Bool := row c P σ a ins = .shaped && c.charge

def next : St :=
  { σ with
    items := σ.items ++ [⟨a.node, if c.run then O c σ a ins else [], out a ins⟩]
    held := σ.held ++ ins.map (fun i => (a.node, i.label)) ++ [(a.node, O c σ a ins)]
    ledger := fun s => if paid c P σ a ins && (O c σ a ins).contains s then σ.ledger s - b a else σ.ledger s
    spent := fun s => if paid c P σ a ins && (O c σ a ins).contains s then σ.spent s + b a else σ.spent s
    bound := fun m => if m = a.node then
        (if c.queue && pub ins then finish c P σ a ins w0 else max a.t (σ.bound a.node) + timeout c P σ a ins)
      else σ.bound m
    tl := fun m => if m = a.node then O c σ a ins else σ.tl m
    free := fun m => if m = a.node then finish c P σ a ins else σ.free m
    cache := fun m => if m = a.node then σ.cache a.node ++ [⟨O c σ a ins, prompt a ins⟩] else σ.cache m }

def obs (w : World) : Obs :=
  match row c P σ a ins, a.ret with
  | .full, _ => .full (out a ins w) (finish c P σ a ins w)
  | .shaped, some (k, pick) =>
    let x := if c.shape then clamp k (pick (out a ins w)) else pick (out a ins w)
    if c.clock then .shaped (if finish c P σ a ins w ≤ a.t + P.deadline then x else k) (a.t + P.deadline)
    else .shaped x (finish c P σ a ins w)
  | _, _ => .receipt a.t

def ok : Bool := P.model a.node && (!c.move || (L ins).all (P.trust a.node)) && shapeOk P a.ret

end Step

/-- One call to the door. Everything here except `free`, item values and the observation is
independent of the world: decisions use labels, the ledger and the queue's bounds only. -/
def step (c : Cfg) (P : Policy) (σ : St) (a : Act) : St × Meta × (World → Obs) :=
  match getAll σ.items a.inputs with
  | none => (σ, ⟨.refused, [], 0⟩, fun _ => .refused)
  | some ins =>
    if Step.ok c P a ins then
      (Step.next c P σ a ins,
       ⟨Step.row c P σ a ins, Step.O c σ a ins, if Step.row c P σ a ins = .shaped then Step.b a else 0⟩,
       Step.obs c P σ a ins)
    else (σ, ⟨.refused, [], 0⟩, fun _ => .refused)

def init (P : Policy) (C : List Item) : St :=
  ⟨C, [], P.allowance, fun _ => 0, fun _ => 0, fun _ => [], fun _ _ => 0, fun _ => []⟩

def hits (S L : Label) : Bool := L.any (S.contains ·)

def ans : Obs → Nat
  | .shaped a _ => a
  | _ => 0

/-- The cloud's run: its history, and a code that collects every shaped answer touching S,
packed in the bits paid for it. -/
structure Run where
  σ : St
  h : List Obs
  code : Nat
  used : Nat

def runS (c : Cfg) (P : Policy) (C : List Item) (strat : List Obs → Act) (S : Label) (w : World) : Nat → Run
  | 0 => ⟨init P C, [], 0, 0⟩
  | k + 1 =>
    let R := runS c P C strat S w k
    let r := step c P R.σ (strat R.h)
    let o := r.2.2 w
    if r.2.1.row = .shaped ∧ hits S r.2.1.label = true then ⟨r.1, R.h ++ [o], R.code + ans o * 2 ^ R.used, R.used + r.2.1.bits⟩
    else ⟨r.1, R.h ++ [o], R.code, R.used⟩

def sumS (S : Label) (g : Scope → Nat) : Nat := (S.map g).sum

/-! ## Proofs -/

theorem bits_go (k : Nat) : ∀ f b, k < 2 ^ (b + f) → k < 2 ^ bits.go k f b
  | 0, b, h => by simpa [bits.go] using h
  | f + 1, b, h => by
    unfold bits.go
    split
    · assumption
    · exact bits_go k f (b + 1) (by rw [show b + 1 + f = b + (f + 1) by omega]; exact h)

/-- Every answer index 0..k fits in the bits paid for it. -/
theorem lt_bits (k : Nat) : k < 2 ^ bits k := by
  unfold bits; exact bits_go k k 0 (by simpa using Nat.lt_two_pow_self)

theorem getAll_mem {items : List Item} : ∀ {is : List Nat} {ins : List Item},
    getAll items is = some ins → ∀ x ∈ ins, x ∈ items
  | [], ins, h => by simp [getAll] at h; subst h; simp
  | i :: is, ins, h => by
    unfold getAll at h
    split at h
    · rename_i x xs hx hxs
      cases h
      intro y hy
      rcases List.mem_cons.mp hy with rfl | hy
      · exact List.mem_of_getElem? hx
      · exact getAll_mem hxs y hy
    · cases h

theorem agreeOn_mono {L M : Label} {w w' : World} (hLM : ∀ s ∈ L, s ∈ M) (h : agreeOn M w w') :
    agreeOn L w w' := fun s hs => h s (hLM s hs)

theorem dependsOn_mono {α : Type} {g : World → α} {L M : Label} (hLM : ∀ s ∈ L, s ∈ M)
    (h : dependsOn g L) : dependsOn g M := fun w w' hw => h w w' (agreeOn_mono hLM hw)

theorem dependsOn_nil {α : Type} {g : World → α} (h : dependsOn g []) (w w' : World) : g w = g w' :=
  h w w' (fun s hs => by simp at hs)

theorem any_congr {α : Type} {l : List α} {p q : α → Bool} (h : ∀ x ∈ l, p x = q x) : l.any p = l.any q := by
  induction l with
  | nil => rfl
  | cons x xs ih =>
    simp only [List.any_cons]
    rw [h x (by simp), ih (fun y hy => h y (by simp [hy]))]

theorem sameSet_sub {K O : Label} (h : sameSet K O = true) : ∀ s ∈ K, s ∈ O := by
  intro s hs
  simp only [sameSet, Bool.and_eq_true, List.all_eq_true, List.contains_iff_mem] at h
  exact h.1 s hs

theorem canCharge_spec {P : Policy} {σ : St} {O : Label} {b : Nat} (h : canCharge P σ O b = true) :
    P.sealed ∉ O ∧ ∀ s ∈ O, b ≤ σ.ledger s := by
  simp only [canCharge, Bool.and_eq_true, Bool.not_eq_true', List.all_eq_true, decide_eq_true_eq] at h
  refine ⟨fun hs => ?_, h.2⟩
  have := h.1
  simp [hs] at this

theorem rowOf_full {c P σ O r} (h : rowOf c P σ O r = .full) : O = [] := by
  unfold rowOf at h
  split at h <;> (split at h <;> first | assumption | (try split at h) <;> simp at h)

theorem rowOf_nil {c P σ r} : rowOf c P σ [] r = .full := by
  unfold rowOf; split <;> simp

theorem rowOf_shaped {c P σ O r} (h : rowOf c P σ O r = .shaped) :
    ∃ k pick, r = some (k, pick) ∧ O ≠ [] ∧ (c.charge = true → canCharge P σ O (bits k) = true) := by
  unfold rowOf at h
  split at h
  · split at h <;> simp at h
  · rename_i k pick
    split at h
    · simp at h
    · rename_i hO
      split at h
      · rename_i hc
        refine ⟨k, pick, rfl, hO, fun hch => ?_⟩
        simpa [hch] using hc
      · simp at h

/-- What every reachable state satisfies (for the real door). -/
structure Inv (P : Policy) (σ : St) : Prop where
  items : ∀ it ∈ σ.items, dependsOn it.val it.label
  held : ∀ p ∈ σ.held, ∀ s ∈ p.2, P.trust p.1 s = true
  held_model : ∀ p ∈ σ.held, P.model p.1 = true
  tl : ∀ m, ∀ s ∈ σ.tl m, P.trust m s = true
  free_le : ∀ m w, σ.free m w ≤ σ.bound m
  free_dep : ∀ m, dependsOn (σ.free m) (σ.tl m)
  cache : ∀ m, ∀ e ∈ σ.cache m, dependsOn e.prompt e.key
  budget : ∀ s, σ.spent s + σ.ledger s = P.allowance s
  sealed : σ.spent P.sealed = 0

theorem init_inv (P : Policy) (C : List Item) (hC : ∀ it ∈ C, dependsOn it.val it.label) :
    Inv P (init P C) where
  items := hC
  held := by simp [init]
  held_model := by simp [init]
  tl := by simp [init]
  free_le := by simp [init]
  free_dep := fun _ _ _ _ => rfl
  cache := by simp [init]
  budget := by simp [init]
  sealed := rfl

/-! ### One step of the real door -/

abbrev real : Cfg := {}

@[simp] theorem real_queue : real.queue = true := rfl
@[simp] theorem real_salt : real.salt = true := rfl
@[simp] theorem real_clock : real.clock = true := rfl
@[simp] theorem real_charge : real.charge = true := rfl
@[simp] theorem real_shape : real.shape = true := rfl
@[simp] theorem real_run : real.run = true := rfl
@[simp] theorem real_move : real.move = true := rfl

section step
variable {P : Policy} {σ : St} {a : Act} {ins : List Item}

theorem L_sub_O : ∀ s ∈ Step.L ins, s ∈ Step.O c σ a ins := fun s hs => by
  simp [Step.O, hs]

theorem taint_trusted (hI : Inv P σ) : ∀ s ∈ Step.taint c σ a ins, P.trust a.node s = true := by
  intro s hs
  unfold Step.taint at hs
  split at hs
  · exact hI.tl _ s hs
  · simp at hs

theorem ins_sound (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    ∀ i ∈ ins, dependsOn i.val i.label := fun i hi => hI.items i (getAll_mem hins i hi)

theorem vals_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (fun w => ins.map (fun i => i.val w)) (Step.L ins) := by
  intro w w' hw
  apply List.map_congr_left
  intro i hi
  exact ins_sound hI hins i hi w w' (agreeOn_mono (fun s hs => List.mem_flatMap.mpr ⟨i, hi, hs⟩) hw)

theorem prompt_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (Step.prompt a ins) (Step.O c σ a ins) := by
  intro w w' hw
  have h : ins.map (fun i => i.val w) = ins.map (fun i => i.val w') := vals_dep hI hins w w' (agreeOn_mono L_sub_O hw)
  simp only [Step.prompt, h]

theorem out_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (Step.out a ins) (Step.O c σ a ins) := by
  intro w w' hw
  have h : ins.map (fun i => i.val w) = ins.map (fun i => i.val w') := vals_dep hI hins w w' (agreeOn_mono L_sub_O hw)
  simp only [Step.out, h]

theorem hit_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (Step.hit real σ a ins) (Step.O real σ a ins) := by
  intro w w' hw
  unfold Step.hit
  apply any_congr
  intro e he
  by_cases hs : sameSet e.key (Step.O real σ a ins) = true
  · have h1 := hI.cache _ e he w w' (agreeOn_mono (sameSet_sub hs) hw)
    have h2 := prompt_dep (c := real) hI hins w w' hw
    simp [hs, h1, h2]
  · simp [hs]

theorem start_dep (hI : Inv P σ) : dependsOn (Step.start real σ a ins) (Step.O real σ a ins) := by
  intro w w' hw
  unfold Step.start
  by_cases hp : Step.pub ins = true
  · simp [hp]
  · simp only [Bool.not_eq_true] at hp
    simp only [hp, Bool.and_false, Bool.false_eq_true, ↓reduceIte]
    by_cases ht : a.t < σ.bound a.node
    · have htaint : Step.taint real σ a ins = σ.tl a.node := by simp [Step.taint, hp, ht]
      have := hI.free_dep a.node w w' (agreeOn_mono (fun s hs => by
        simp only [Step.O, List.mem_append]; right; rw [htaint]; exact hs) hw)
      simp [this]
    · have h1 := hI.free_le a.node w
      have h2 := hI.free_le a.node w'
      omega

theorem finish_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (Step.finish real P σ a ins) (Step.O real σ a ins) := by
  intro w w' hw
  unfold Step.finish
  rw [start_dep hI w w' hw, prompt_dep hI hins w w' hw, hit_dep hI hins w w' hw]

theorem pub_O (hp : Step.pub ins = true) : Step.O real σ a ins = [] := by
  have hL : Step.L ins = [] := by simpa [Step.pub] using hp
  simp [Step.O, Step.taint, hp, hL]

theorem trusted_O (hI : Inv P σ) (hok : Step.ok real P a ins = true) :
    ∀ s ∈ Step.O real σ a ins, P.trust a.node s = true := by
  have hL : ∀ s ∈ Step.L ins, P.trust a.node s = true := by
    simp only [Step.ok, Bool.and_eq_true] at hok
    simpa using hok.1.2
  intro s hs
  simp only [Step.O, List.mem_append] at hs
  rcases hs with hs | hs
  · exact hL s hs
  · exact taint_trusted hI s hs

theorem paid_spec (hp : Step.paid real P σ a ins = true) :
    P.sealed ∉ Step.O real σ a ins ∧ ∀ s ∈ Step.O real σ a ins, Step.b a ≤ σ.ledger s := by
  have hrow : Step.row real P σ a ins = .shaped := by simpa [Step.paid] using hp
  obtain ⟨k, pick, hr, _, hc⟩ := rowOf_shaped hrow
  have hb : Step.b a = bits k := by simp [Step.b, hr]
  rw [hb]
  exact canCharge_spec (hc rfl)

theorem next_inv (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins)
    (hok : Step.ok real P a ins = true) : Inv P (Step.next real P σ a ins) where
  items := by
    intro it hit
    simp only [Step.next, List.mem_append, List.mem_singleton] at hit
    rcases hit with hit | rfl
    · exact hI.items it hit
    · exact out_dep hI hins
  held := by
    intro p hp s hs
    simp only [Step.next, List.mem_append, List.mem_map, List.mem_singleton] at hp
    rcases hp with (hp | ⟨i, hi, rfl⟩) | rfl
    · exact hI.held p hp s hs
    · exact trusted_O hI hok s (L_sub_O s (List.mem_flatMap.mpr ⟨i, hi, hs⟩))
    · exact trusted_O hI hok s hs
  held_model := by
    intro p hp
    have hm : P.model a.node = true := by
      simp only [Step.ok, Bool.and_eq_true] at hok; exact hok.1.1
    simp only [Step.next, List.mem_append, List.mem_map, List.mem_singleton] at hp
    rcases hp with (hp | ⟨i, _, rfl⟩) | rfl
    · exact hI.held_model p hp
    · exact hm
    · exact hm
  tl := by
    intro m s hs
    simp only [Step.next] at hs
    split at hs
    · subst m; exact trusted_O hI hok s hs
    · exact hI.tl m s hs
  free_le := by
    intro m w
    simp only [Step.next]
    split
    · subst m
      by_cases hp : Step.pub ins = true
      · simp only [hp, Bool.and_self, ↓reduceIte]
        exact Nat.le_of_eq (finish_dep hI hins w w0 (by rw [pub_O hp]; intro s hs; simp at hs))
      · simp only [hp, Bool.and_false, Bool.false_eq_true, ↓reduceIte]
        have := hI.free_le a.node w
        unfold Step.finish Step.start
        simp only [hp, Bool.and_false, Bool.false_eq_true, ↓reduceIte]
        omega
    · exact hI.free_le m w
  free_dep := by
    intro m
    simp only [Step.next]
    split
    · exact finish_dep hI hins
    · exact hI.free_dep m
  cache := by
    intro m e he
    simp only [Step.next] at he
    split at he
    · subst m
      simp only [List.mem_append, List.mem_singleton] at he
      rcases he with he | rfl
      · exact hI.cache _ e he
      · exact prompt_dep hI hins
    · exact hI.cache m e he
  budget := by
    intro s
    simp only [Step.next]
    by_cases h : (Step.paid real P σ a ins && (Step.O real σ a ins).contains s) = true
    · simp only [h, ↓reduceIte]
      simp only [Bool.and_eq_true, List.contains_iff_mem] at h
      have := (paid_spec h.1).2 s h.2
      have := hI.budget s
      omega
    · simp only [h, Bool.false_eq_true, ↓reduceIte]
      exact hI.budget s
  sealed := by
    simp only [Step.next]
    by_cases h : (Step.paid real P σ a ins && (Step.O real σ a ins).contains P.sealed) = true
    · simp only [Bool.and_eq_true, List.contains_iff_mem] at h
      exact absurd h.2 (paid_spec h.1).1
    · simp only [h, Bool.false_eq_true, ↓reduceIte]
      exact hI.sealed

end step

section step2
variable {P : Policy} {σ : St} {a : Act} {ins : List Item}

theorem obs_dep (hI : Inv P σ) (hins : getAll σ.items a.inputs = some ins) :
    dependsOn (Step.obs real P σ a ins) (Step.O real σ a ins) := by
  intro w w' hw
  have h1 := out_dep (c := real) hI hins w w' hw
  have h2 := finish_dep hI hins w w' hw
  unfold Step.obs
  rw [h1, h2]

theorem obs_const (h1 : Step.row real P σ a ins ≠ .shaped) (h2 : Step.row real P σ a ins ≠ .full) :
    ∀ w w', Step.obs real P σ a ins w = Step.obs real P σ a ins w' := by
  intro w w'
  unfold Step.obs
  split <;> simp_all

theorem obs_shaped (hrow : Step.row real P σ a ins = .shaped) :
    ∀ w, ∃ x, Step.obs real P σ a ins w = .shaped x (a.t + P.deadline) ∧ x < 2 ^ Step.b a := by
  obtain ⟨k, pick, hr, _, _⟩ := rowOf_shaped hrow
  intro w
  have hb : Step.b a = bits k := by simp [Step.b, hr]
  have hk := lt_bits k
  unfold Step.obs
  simp only [hrow, hr, ↓reduceIte]
  refine ⟨_, rfl, ?_⟩
  rw [hb]
  simp only [clamp]
  split
  · split <;> omega
  · omega

/-- Everything the run-level proofs need from one call. -/
theorem step_facts (hI : Inv P σ) (a : Act) :
    Inv P (step real P σ a).1 ∧
    dependsOn (step real P σ a).2.2 (step real P σ a).2.1.label ∧
    ((step real P σ a).2.1.row = .full → (step real P σ a).2.1.label = []) ∧
    ((step real P σ a).2.1.row ≠ .shaped → (step real P σ a).2.1.row ≠ .full →
       ∀ w w', (step real P σ a).2.2 w = (step real P σ a).2.2 w') ∧
    ((step real P σ a).2.1.row = .shaped → ∀ w, ∃ x,
       (step real P σ a).2.2 w = .shaped x (a.t + P.deadline) ∧ x < 2 ^ (step real P σ a).2.1.bits) ∧
    (∀ s, σ.spent s ≤ (step real P σ a).1.spent s) ∧
    ((step real P σ a).2.1.row = .shaped → ∀ s ∈ (step real P σ a).2.1.label,
       (step real P σ a).1.spent s = σ.spent s + (step real P σ a).2.1.bits) ∧
    (∀ w, (step real P σ a).2.2 w = .refused ∨ (step real P σ a).2.2 w = .receipt a.t ∨
       (∃ v t, (step real P σ a).2.2 w = .full v t) ∨ ∃ x, (step real P σ a).2.2 w = .shaped x (a.t + P.deadline)) := by
  unfold step
  split
  · exact ⟨hI, fun _ _ _ => rfl, by simp, fun _ _ _ _ => rfl, by simp, fun _ => Nat.le_refl _, by simp,
      fun _ => Or.inl rfl⟩
  · rename_i ins hins
    split
    · rename_i hok
      dsimp only
      refine ⟨next_inv hI hins hok, obs_dep hI hins, fun h => rowOf_full h, fun h1 h2 => obs_const h1 h2,
        fun h => by simp only [h, ↓reduceIte]; exact obs_shaped h, ?_, ?_, ?_⟩
      · intro s; simp only [Step.next]; split <;> omega
      · intro h s hs
        have hp : Step.paid real P σ a ins = true := by simp [Step.paid, h]
        simp [Step.next, hp, hs, h]
      · intro w
        by_cases hsh : Step.row real P σ a ins = .shaped
        · obtain ⟨x, hx, _⟩ := obs_shaped hsh w
          exact Or.inr (Or.inr (Or.inr ⟨x, hx⟩))
        · unfold Step.obs
          split
          · exact Or.inr (Or.inr (Or.inl ⟨_, _, rfl⟩))
          · simp_all
          · exact Or.inr (Or.inl rfl)
    · exact ⟨hI, fun _ _ _ => rfl, by simp, fun _ _ _ _ => rfl, by simp, fun _ => Nat.le_refl _, by simp,
        fun _ => Or.inl rfl⟩

theorem step_sealed (a : Act) (h : (step real P σ a).2.1.row = .shaped) : P.sealed ∉ (step real P σ a).2.1.label := by
  revert h
  unfold step
  split
  · simp
  · rename_i ins hins
    split
    · intro h
      exact (paid_spec (by simp [Step.paid]; exact h)).1
    · simp

end step2

/-! ### The whole run, against any strategy -/

theorem sumS_mono {S : Label} {g g' : Scope → Nat} (h : ∀ s, g s ≤ g' s) : sumS S g ≤ sumS S g' := by
  induction S with
  | nil => simp [sumS]
  | cons x xs ih => simp only [sumS, List.map_cons, List.sum_cons] at ih ⊢; have := h x; omega

theorem sumS_bump {S : Label} {g g' : Scope → Nat} {b : Nat} (h : ∀ s, g s ≤ g' s) {s₀ : Scope}
    (hs : s₀ ∈ S) (hb : g' s₀ = g s₀ + b) : sumS S g + b ≤ sumS S g' := by
  induction S with
  | nil => simp at hs
  | cons x xs ih =>
    simp only [sumS, List.map_cons, List.sum_cons] at ih ⊢
    rcases List.mem_cons.mp hs with rfl | hs
    · have := sumS_mono (S := xs) h; simp only [sumS] at this; omega
    · have := ih hs; have := h x; omega

section run
variable {P : Policy} {C : List Item} {strat : List Obs → Act} {S : Label}

theorem runS_succ (w : World) (k : Nat) :
    let R := runS real P C strat S w k
    let r := step real P R.σ (strat R.h)
    (runS real P C strat S w (k + 1)).σ = r.1 ∧
    (runS real P C strat S w (k + 1)).h = R.h ++ [r.2.2 w] ∧
    (runS real P C strat S w (k + 1)).code =
      (if r.2.1.row = .shaped ∧ hits S r.2.1.label = true then R.code + ans (r.2.2 w) * 2 ^ R.used else R.code) ∧
    (runS real P C strat S w (k + 1)).used =
      (if r.2.1.row = .shaped ∧ hits S r.2.1.label = true then R.used + r.2.1.bits else R.used) := by
  simp only [runS]
  split <;> simp_all

theorem runS_len (w : World) : ∀ k, (runS real P C strat S w k).h.length = k
  | 0 => rfl
  | k + 1 => by rw [(runS_succ w k).2.1]; simp [runS_len w k]

theorem hits_iff {S L : Label} : hits S L = true ↔ ∃ s ∈ L, s ∈ S := by
  simp [hits, List.any_eq_true]

theorem runS_inv (hC : ∀ it ∈ C, dependsOn it.val it.label) (w : World) : ∀ k,
    Inv P (runS real P C strat S w k).σ ∧
    (runS real P C strat S w k).code < 2 ^ (runS real P C strat S w k).used ∧
    (runS real P C strat S w k).used ≤ sumS S (runS real P C strat S w k).σ.spent
  | 0 => ⟨init_inv P C hC, by simp [runS], by simp [runS]⟩
  | k + 1 => by
    obtain ⟨hI, hc, hu⟩ := runS_inv hC w k
    obtain ⟨hσ, -, hcode, hused⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w k
    obtain ⟨hI', -, -, -, hsh, hmono, hbump, -⟩ := step_facts (a := strat (runS real P C strat S w k).h) hI
    refine ⟨hσ ▸ hI', ?_, ?_⟩
    · rw [hcode, hused]
      split
      · rename_i hcond
        obtain ⟨x, hx, hxlt⟩ := hsh hcond.1 w
        rw [hx]
        simp only [ans]
        have h1 : x * 2 ^ (runS real P C strat S w k).used + 2 ^ (runS real P C strat S w k).used ≤
            2 ^ (runS real P C strat S w k).used * 2 ^ (step real P (runS real P C strat S w k).σ
              (strat (runS real P C strat S w k).h)).2.1.bits := by
          rw [← Nat.succ_mul, Nat.mul_comm]; exact Nat.mul_le_mul_left _ hxlt
        rw [Nat.pow_add]; omega
      · exact hc
    · rw [hused, hσ]
      split
      · rename_i hcond
        obtain ⟨s₀, hs₀L, hs₀S⟩ := hits_iff.mp hcond.2
        have := sumS_bump (S := S) hmono hs₀S (hbump hcond.1 s₀ hs₀L)
        omega
      · exact Nat.le_trans hu (sumS_mono hmono)

/-- Same history so far ⇒ same state, code and bits used: the door's state is a function of the calls. -/
theorem runS_det (w w' : World) : ∀ k, (runS real P C strat S w k).h = (runS real P C strat S w' k).h →
    (runS real P C strat S w k).σ = (runS real P C strat S w' k).σ ∧
    (runS real P C strat S w k).code = (runS real P C strat S w' k).code ∧
    (runS real P C strat S w k).used = (runS real P C strat S w' k).used
  | 0, _ => ⟨rfl, rfl, rfl⟩
  | k + 1, hh => by
    obtain ⟨hσ, hh1, hc, hu⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w k
    obtain ⟨hσ', hh1', hc', hu'⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w' k
    rw [hh1, hh1'] at hh
    obtain ⟨hhk, ho⟩ := List.append_inj hh (by rw [runS_len, runS_len])
    obtain ⟨eσ, ec, eu⟩ := runS_det w w' k hhk
    simp only [List.cons.injEq, and_true] at ho
    rw [hσ, hσ', hc, hc', hu, hu', eσ, ec, eu, hhk]
    rw [eσ, hhk] at ho
    exact ⟨rfl, by rw [ho], rfl⟩

theorem runS_digits (w : World) (j : Nat) : ∀ d,
    (runS real P C strat S w j).used ≤ (runS real P C strat S w (j + d)).used ∧
    ∃ q, (runS real P C strat S w (j + d)).code =
      (runS real P C strat S w j).code + 2 ^ (runS real P C strat S w j).used * q
  | 0 => ⟨Nat.le_refl _, 0, by simp⟩
  | d + 1 => by
    obtain ⟨hle, q, hq⟩ := runS_digits w j d
    obtain ⟨-, -, hc, hu⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w (j + d)
    rw [show j + (d + 1) = j + d + 1 by omega, hc, hu]
    split
    · refine ⟨by omega, q + ans ((step real P (runS real P C strat S w (j + d)).σ
          (strat (runS real P C strat S w (j + d)).h)).2.2 w) * 2 ^ ((runS real P C strat S w (j + d)).used -
          (runS real P C strat S w j).used), ?_⟩
      have e : 2 ^ (runS real P C strat S w (j + d)).used = 2 ^ (runS real P C strat S w j).used *
          2 ^ ((runS real P C strat S w (j + d)).used - (runS real P C strat S w j).used) := by
        rw [← Nat.pow_add]; congr 1; omega
      rw [hq, e, Nat.mul_add]
      ac_rfl
    · exact ⟨hle, q, hq⟩

theorem code_mod (hC : ∀ it ∈ C, dependsOn it.val it.label) (w : World) {j n : Nat} (hjn : j ≤ n) :
    (runS real P C strat S w j).code =
      (runS real P C strat S w n).code % 2 ^ (runS real P C strat S w j).used := by
  obtain ⟨-, q, hq⟩ := runS_digits (P := P) (C := C) (strat := strat) (S := S) w j (n - j)
  rw [show j + (n - j) = n by omega] at hq
  rw [hq, Nat.add_mul_mod_self_left, Nat.mod_eq_of_lt (runS_inv hC w j).2.1]

theorem runS_prefix (hC : ∀ it ∈ C, dependsOn it.val it.label) (w w' : World)
    (hw : ∀ t, t ∉ S → w t = w' t) (n : Nat)
    (hcode : (runS real P C strat S w n).code = (runS real P C strat S w' n).code) :
    ∀ k, k ≤ n → (runS real P C strat S w k).h = (runS real P C strat S w' k).h
  | 0, _ => rfl
  | k + 1, hk => by
    have hh := runS_prefix hC w w' hw n hcode k (by omega)
    obtain ⟨eσ, ec, eu⟩ := runS_det (P := P) (C := C) (strat := strat) (S := S) w w' k hh
    obtain ⟨-, hh1, hc1, hu1⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w k
    obtain ⟨-, hh1', hc1', hu1'⟩ := runS_succ (P := P) (C := C) (strat := strat) (S := S) w' k
    rw [hh1, hh1', ← eσ, ← hh]
    rw [← eσ, ← hh, ← ec, ← eu] at hc1'
    rw [← eσ, ← hh, ← eu] at hu1'
    obtain ⟨-, hdep, hfull, hconst, hsh, -, -, -⟩ := step_facts (a := strat (runS real P C strat S w k).h)
      (runS_inv (P := P) (C := C) (strat := strat) (S := S) hC w k).1
    congr 2
    by_cases hS : hits S (step real P (runS real P C strat S w k).σ (strat (runS real P C strat S w k).h)).2.1.label = true
    · by_cases hrow : (step real P (runS real P C strat S w k).σ (strat (runS real P C strat S w k).h)).2.1.row = .shaped
      · obtain ⟨x, hx, -⟩ := hsh hrow w
        obtain ⟨x', hx', -⟩ := hsh hrow w'
        have m1 := code_mod (P := P) (C := C) (strat := strat) (S := S) hC w (show k + 1 ≤ n by omega)
        have m2 := code_mod (P := P) (C := C) (strat := strat) (S := S) hC w' (show k + 1 ≤ n by omega)
        have hu : (runS real P C strat S w (k + 1)).used = (runS real P C strat S w' (k + 1)).used := by
          rw [hu1, hu1']
        have hck : (runS real P C strat S w (k + 1)).code = (runS real P C strat S w' (k + 1)).code := by
          rw [m1, m2, hcode, hu]
        rw [hc1, hc1'] at hck
        simp only [hrow, hS, and_self, ↓reduceIte, hx, hx', ans] at hck
        have : x = x' := Nat.eq_of_mul_eq_mul_right (Nat.two_pow_pos (runS real P C strat S w k).used) (by omega)
        rw [hx, hx', this]
      · have hnf : (step real P (runS real P C strat S w k).σ (strat (runS real P C strat S w k).h)).2.1.row ≠ .full := by
          intro hf
          obtain ⟨s, hs, -⟩ := hits_iff.mp hS
          rw [hfull hf] at hs; simp at hs
        exact hconst hrow hnf w w'
    · apply hdep
      intro s hs
      apply hw
      intro hsS
      exact hS (hits_iff.mpr ⟨s, hs, hsS⟩)

end run

/-! ## The theorems -/

section main
variable (P : Policy) (C : List Item) (hC : ∀ it ∈ C, dependsOn it.val it.label)
  (strat : List Obs → Act) (S : Label) (n : Nat)
include hC

/-- **Leak bound.** Whatever the strategy, after any number of calls the cloud's whole history is
a function of a code below 2 ^ (Σ_{s∈S} allowance s), as long as only the secrets in S vary. So the
cloud can tell apart at most that many worlds that differ in S: it learns at most Σ allowance bits
about S, through answers, reply times, the queue and the prompt cache together. -/
theorem leak_bound :
    (∀ w, (runS real P C strat S w n).code < 2 ^ sumS S P.allowance) ∧
    (∀ w w', (∀ t, t ∉ S → w t = w' t) →
      (runS real P C strat S w n).code = (runS real P C strat S w' n).code →
      (runS real P C strat S w n).h = (runS real P C strat S w' n).h) := by
  refine ⟨fun w => ?_, fun w w' hw hc => runS_prefix hC w w' hw n hc n (Nat.le_refl n)⟩
  obtain ⟨hI, hc, hu⟩ := runS_inv (P := P) (C := C) (strat := strat) (S := S) hC w n
  have : sumS S (runS real P C strat S w n).σ.spent ≤ sumS S P.allowance :=
    sumS_mono (fun s => by have := hI.budget s; omega)
  exact Nat.lt_of_lt_of_le hc (Nat.pow_le_pow_right (by decide) (by omega))

/-- **Sealed.** Worlds that differ only in the sealed scope look the same to the cloud. -/
theorem sealed_hidden (w w' : World) (hw : ∀ t, t ≠ P.sealed → w t = w' t) :
    (runS real P C strat [P.sealed] w n).h = (runS real P C strat [P.sealed] w' n).h := by
  have z : ∀ v, (runS real P C strat [P.sealed] v n).code = 0 := fun v => by
    obtain ⟨hI, hc, hu⟩ := runS_inv (P := P) (C := C) (strat := strat) (S := [P.sealed]) hC v n
    simp only [sumS, List.map_cons, List.map_nil, List.sum_cons, List.sum_nil, hI.sealed] at hu
    rw [show (runS real P C strat [P.sealed] v n).used = 0 by omega] at hc
    omega
  exact (leak_bound P C hC strat [P.sealed] n).2 w w' (fun t ht => hw t (by simpa using ht)) (by rw [z, z])

/-- **Containment.** Every copy and every output the door places is on a model node trusted with
all of its scopes; with a well-formed policy, sealed data is never placed anywhere. -/
theorem held_trusted (hP : P.WF) (w : World) :
    ∀ p ∈ (runS real P C strat S w n).σ.held,
      P.model p.1 = true ∧ (∀ s ∈ p.2, P.trust p.1 s = true) ∧ P.sealed ∉ p.2 := by
  intro p hp
  have hI := (runS_inv (P := P) (C := C) (strat := strat) (S := S) hC w n).1
  refine ⟨hI.held_model p hp, hI.held p hp, fun hs => ?_⟩
  have := hI.held p hp _ hs
  rw [hP.sealed_no_model _ (hI.held_model p hp)] at this
  simp at this

/-- **Budget.** Bits spent on a scope plus what is left is always its allowance; sealed is never paid. -/
theorem budget (w : World) :
    (∀ s, (runS real P C strat S w n).σ.spent s + (runS real P C strat S w n).σ.ledger s = P.allowance s) ∧
    (runS real P C strat S w n).σ.spent P.sealed = 0 :=
  let hI := (runS_inv (P := P) (C := C) (strat := strat) (S := S) hC w n).1
  ⟨hI.budget, hI.sealed⟩

omit hC in
/-- **Clock.** Each reply is a refusal, a receipt at once, a full public output, or a shaped answer
exactly `deadline` after its call. -/
theorem reply_times (w : World) (σ : St) (hI : Inv P σ) (a : Act) :
    (step real P σ a).2.2 w = .refused ∨ (step real P σ a).2.2 w = .receipt a.t ∨
    (∃ v t, (step real P σ a).2.2 w = .full v t) ∨ ∃ x, (step real P σ a).2.2 w = .shaped x (a.t + P.deadline) :=
  (step_facts hI a).2.2.2.2.2.2.2 w

end main

/-! ## Every rule is load-bearing

For each rule, the door with that rule switched off, on a small policy that satisfies `Policy.WF`:
two worlds that differ only in the hr secret (scope 1), and a fixed plan of calls, after which the
cloud's histories differ where `leak_bound` (or `held_trusted`) forbids it. Each is checked by
evaluation (`decide`). The real door on the same plan is shown to keep the worlds apart. -/

namespace Mutants

/-- Node 1 has a model and is trusted with hr (scope 1); node 2 has a model and is trusted with
nothing; scope 0 is sealed. -/
def pol (allow : Nat) : Policy where
  trust := fun n s => n == 1 && s == 1
  model := fun n => n == 1 || n == 2
  allowance := fun s => if s = 1 then allow else 0
  sealed := 0
  deadline := 30
  jobTimeout := 100
  maxBits := 16

theorem pol_wf (k : Nat) : (pol k).WF := ⟨fun n _ => by simp [pol]⟩

/-- The hr file on node 1: its value is the hr secret. -/
def cat : List Item := [⟨1, [1], fun w => w 1⟩]

theorem cat_sound : ∀ it ∈ cat, dependsOn it.val it.label := by
  intro it hit w w' hw
  simp only [cat, List.mem_singleton] at hit
  subst hit
  exact hw 1 (by simp)

def wA : World := fun _ => 0
def wB : World := fun s => if s = 1 then 1 else 0

theorem differ_in_hr : ∀ t, t ∉ [1] → wA t = wB t := by
  intro t ht; simp at ht; simp [wA, wB, ht]

def call (t node : Nat) (inputs task : List Nat) (dur : List Nat → Bool → Nat)
    (ret : Option (Nat × (Nat → Nat)) := none) (f : List Nat → Nat := fun _ => 0) : Act :=
  ⟨t, node, inputs, task, f, dur, ret⟩

/-- A fixed plan: the i-th call, whatever came back before. -/
def plan (cs : List Act) : List Obs → Act := fun h => cs.getD h.length (call 0 1 [] [] (fun _ _ => 0))

def hist (c : Cfg) (allow : Nat) (cs : List Act) (w : World) : List Obs :=
  (runS c (pol allow) cat (plan cs) [1] w cs.length).h

def code (c : Cfg) (allow : Nat) (cs : List Act) (w : World) : Nat :=
  (runS c (pol allow) cat (plan cs) [1] w cs.length).code

/-- The secret sets the length of a free private job; then a public job on the same node. -/
def contention : List Act :=
  [call 0 1 [0] [] (fun p _ => if p = [1] then 50 else 1),
   call 0 1 [] [7] (fun _ _ => 1)]

/-- A free private job leaves its prompt in the cache; later, a public job guesses it. -/
def cacheGuess : List Act :=
  [call 0 1 [0] [] (fun _ _ => 1),
   call 500 1 [] [1] (fun _ hit => if hit then 1 else 5)]

/-- A paid one-bit answer that is always 0, from a job whose length depends on the secret. -/
def slowAnswer : List Act :=
  [call 0 1 [0] [] (fun p _ => if p = [1] then 20 else 1) (some (1, fun _ => 0))]

/-- Ask for the secret itself as a two-valued answer. -/
def askSecret : List Act :=
  [call 0 1 [0] [] (fun _ _ => 1) (some (2, id)) (fun v => v.headD 0)]

/-- A one-answer schema, but the model returns 5 × the secret. -/
def overflow : List Act :=
  [call 0 1 [0] [] (fun _ _ => 1) (some (1, fun v => 5 * v)) (fun v => v.headD 0)]

/-- Copy the secret into an output for free, then use that output as a public input. -/
def launder : List Act :=
  [call 0 1 [0] [] (fun _ _ => 1) none (fun v => v.headD 0),
   call 500 1 [1] [] (fun _ _ => 1) none (fun v => v.headD 0)]

/-- Run a job on the hr file on node 2, which is not trusted with hr. -/
def moveOut : List Act := [call 0 2 [0] [] (fun _ _ => 1)]

-- With no allowance at all, the real door shows the cloud the same history in both worlds ...
example : hist real 0 contention wA = hist real 0 contention wB := by decide
example : hist real 0 cacheGuess wA = hist real 0 cacheGuess wB := by decide
example : hist real 0 askSecret wA = hist real 0 askSecret wB := by decide
example : hist real 0 launder wA = hist real 0 launder wB := by decide
-- ... and with one bit, it answers at the deadline whatever the job took.
example : hist real 1 slowAnswer wA = hist real 1 slowAnswer wB := by decide

/-- No queue: the public job's reply time reveals the secret, with no allowance. -/
theorem mutant_queue : hist { queue := false } 0 contention wA ≠ hist { queue := false } 0 contention wB := by decide
/-- No salt: a cache hit reveals the secret, with no allowance. -/
theorem mutant_salt : hist { salt := false } 0 cacheGuess wA ≠ hist { salt := false } 0 cacheGuess wB := by decide
/-- No clock: the same answer (0) and the same code, but the reply time reveals the secret. -/
theorem mutant_clock : code { clock := false } 1 slowAnswer wA = code { clock := false } 1 slowAnswer wB ∧
    hist { clock := false } 1 slowAnswer wA ≠ hist { clock := false } 1 slowAnswer wB := by decide
/-- No charge: the secret comes back as an answer, with no allowance. -/
theorem mutant_charge : hist { charge := false } 0 askSecret wA ≠ hist { charge := false } 0 askSecret wB := by decide
/-- No shape: an answer outside its declared set carries more than the bits paid. -/
theorem mutant_shape : code { shape := false } 1 overflow wB ≥ 2 ^ sumS [1] (pol 1).allowance := by decide
/-- No run rule: an output loses its scopes, and the secret comes back in full, with no allowance. -/
theorem mutant_run : hist { run := false } 0 launder wA ≠ hist { run := false } 0 launder wB := by decide
/-- No move rule: hr bytes are placed on a node not trusted with hr. -/
theorem mutant_move : ∃ p ∈ (runS { move := false } (pol 0) cat (plan moveOut) [1] wA 1).σ.held,
    ∃ s ∈ p.2, (pol 0).trust p.1 s = false := by decide

end Mutants

/-! ## Twin: run the model on call sequences, for twin.py to compare with the real door -/

namespace Twin

def nats (ws : List String) : List Nat := ws.filterMap String.toNat?

structure Case where
  deadline : Nat := 30
  jobTimeout : Nat := 100
  maxBits : Nat := 16
  nodes : List (Nat × Bool × List Nat) := []
  allow : List (Nat × Nat) := []
  items : List (Nat × List Nat) := []
  calls : List (Nat × Nat × Nat × Nat × List Nat) := []    -- t, node, k (0 = no returns), dur, inputs

def Case.policy (c : Case) : Policy where
  trust := fun n s => c.nodes.any (fun (m, _, ts) => m == n && ts.contains s)
  model := fun n => c.nodes.any (fun (m, md, _) => m == n && md)
  allowance := fun s => ((c.allow.find? (·.1 == s)).map (·.2)).getD 0
  sealed := 0
  deadline := c.deadline
  jobTimeout := c.jobTimeout
  maxBits := c.maxBits

def Case.act (call : Nat × Nat × Nat × Nat × List Nat) : Act :=
  let (t, n, k, d, ins) := call
  ⟨t, n, ins, [], fun _ => 0, fun _ _ => d, if k = 0 then none else some (k, fun _ => 0)⟩

def parse (lines : List String) : List Case := Id.run do
  let mut out : List Case := []
  let mut c : Case := {}
  for line in lines do
    match line.splitOn " " |>.filter (· ≠ "") with
    | "case" :: _ => c := {}
    | "policy" :: rest => match nats rest with
      | [d, j, m] => c := { c with deadline := d, jobTimeout := j, maxBits := m }
      | _ => pure ()
    | "node" :: rest => match nats rest with
      | n :: md :: ts => c := { c with nodes := c.nodes ++ [(n, md == 1, ts)] }
      | _ => pure ()
    | "allow" :: rest => match nats rest with
      | [s, b] => c := { c with allow := c.allow ++ [(s, b)] }
      | _ => pure ()
    | "item" :: rest => match nats rest with
      | n :: ss => c := { c with items := c.items ++ [(n, ss)] }
      | _ => pure ()
    | "call" :: rest => match nats rest with
      | t :: n :: k :: d :: ins => c := { c with calls := c.calls ++ [(t, n, k, d, ins)] }
      | _ => pure ()
    | "end" :: _ => out := out ++ [c]
    | _ => pure ()
  return out

def showL (L : Label) : String := toString ((L.eraseDups).mergeSort (· ≤ ·))

def rowName : Row → String
  | .refused => "refused" | .receipt => "receipt" | .full => "full" | .shaped => "shaped"

def runCase (c : Case) : List String := Id.run do
  let P := c.policy
  let cat : List Item := c.items.map (fun (n, ss) => ⟨n, ss, fun _ => 0⟩)
  let mut σ := init P cat
  let mut out : List String := []
  for call in c.calls do
    let a := Case.act call
    let (σ', m, _) := step real P σ a
    σ := σ'
    out := out ++ [s!"{rowName m.row} {showL m.label} {m.bits} {(List.range 4).map σ.ledger} {σ.bound a.node} {showL (σ.tl a.node)}"]
  return out

end Twin

def main (args : List String) : IO Unit := do
  let lines ← IO.FS.lines (args.headD "cases.txt")
  for c in Twin.parse lines.toList do
    for l in Twin.runCase c do
      IO.println l
    IO.println "end"

end Lego

def main (args : List String) : IO Unit := Lego.main args
