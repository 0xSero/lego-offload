"""The door's queue with a stub transport (no network): jobs on a node never overlap, run in the
order they arrived, and a public reply's time does not depend on how long private work before
it ran. Times are scaled down: deadline 0.5 s, job limit 1 s."""
import sys
import os
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import door  # noqa: E402
door.measured = lambda node: False                          # stub nodes: no real disk to measure
import transport  # noqa: E402

POLICY = {"host": "mac", "workspace": "/ws", "public": ["/ws/a.py"], "deadline": 0.5, "job_timeout": 1.0,
          "allowance": {"hr": 8}, "scopes": {"mac:/data/hr": "hr"},
          "nodes": {"mac": {"trust": ["hr", "sealed"], "model": False, "out": "/o"},
                    "pop": {"trust": ["hr"], "model": True, "out": "/o", "stage": "/s"}}}
SPANS, LOCK = [], threading.Lock()


def fake_job(node, files, task, returns, out, keep, timeout, salt, **_):
    t0 = time.time()
    time.sleep(min(float(task), timeout))                          # the task text is the job's length
    with LOCK:
        SPANS.append((t0, time.time(), task))
    return {"took": time.time() - t0, "output": "1"}


transport.run_job = fake_job
transport.put = lambda node, stage, name, data, timeout=60: f"{stage}/{name}"
door.Door.bytes_of = lambda self, item, scrubbed, timeout=60, node=None, data=None: b""
transport.put_tree = lambda node, stage, src, timeout=60: f"{stage}/x"   # no real files behind hidden inputs


def trial(private_len):
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.realpath(tmp) + "/ws"
        os.mkdir(ws)
        open(ws + "/a.py", "w").close()
        d = door.Door({**POLICY, "workspace": ws, "public": [ws + "/a.py"]}, tmp + "/state")
        d.offload(str(private_len), ["mac:/data/hr/x.csv"])        # receipt: runs in the background
        t1 = time.time()
        r = d.offload("0.05", ["a.py"])                            # public: waits for the bound
        took = time.time() - t1
        for j in d.jobs:
            j.join()
        return r["row"], took


def test_public_time_independent_of_private():
    times = {}
    for length in (0.05, 0.9, 0.05, 0.9):
        row, took = trial(length)
        assert row == "full"
        times.setdefault(length, []).append(took)
    lo, hi = times[0.05], times[0.9]
    assert all(abs(a - b) < 0.1 for a in lo for b in hi), times     # both wait out the 1 s limit
    assert min(lo + hi) >= 1.0, times


def test_fifo_no_overlap():
    SPANS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.realpath(tmp) + "/ws"
        os.mkdir(ws)
        open(ws + "/a.py", "w").close()
        d = door.Door({**POLICY, "workspace": ws, "public": [ws + "/a.py"]}, tmp + "/state")
        order = ["0.2", "0.1", "0.3", "0.05"]
        for task in order:
            d.offload(task, ["mac:/data/hr/x.csv"])
        for j in d.jobs:
            j.join()
    spans = sorted(SPANS)
    assert [s[2] for s in spans] == order, spans
    assert all(a[1] <= b[0] + 1e-3 for a, b in zip(spans, spans[1:])), spans


def test_shaped_after_private_carries_its_scopes():
    with tempfile.TemporaryDirectory() as tmp:
        d = door.Door({**POLICY, "allowance": {"hr": 8}}, tmp)
        d.offload("0.05", ["mac:/data/hr/x.csv"])
        r = d.offload("0.05", ["mac:/data/hr/x.csv"], {"bool": True})
        for j in d.jobs:
            j.join()
        assert r["row"] == "shaped" and d.slots["pop"][1] == frozenset({"hr"})


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
