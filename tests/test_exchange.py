"""The exchange rule, both ways, with a stub model (no network): what each model is given is what
its level allows, scrubbed text holds no identifier, a model that sees everything gets real names
back, and links or workspace copies do not launder a label."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import door  # noqa: E402
import transport  # noqa: E402

GOT = []                                                       # (node, what the model was given)


def fake_job(node, files, task, returns, out, keep, timeout, salt=None):     # the files are staged on this host
    seen = task + "".join(Path(f).read_text() for f in files)
    GOT.append((node["model"]["model"], seen))
    reply = json.dumps({"answer": True}) if returns else "echo: " + seen
    return {"took": 0.0, "output": None if keep else reply}


transport.run_job = fake_job
CSV = "name,email,salary\nZorbina Quellwater,zq@corp.example,162000\nQuillon Brimstoke,qb@corp.example,118000\n"
IDS = ["Zorbina", "Quellwater", "Quillon", "Brimstoke", "zq@corp.example", "qb@corp.example", "sk_live_Canary123456789012345"]


def setup(tmp):
    t = Path(tmp)
    for d in ("hr", "keys", "ws"):
        (t / d).mkdir()
    (t / "hr/people.csv").write_text(CSV)
    (t / "keys/prod.env").write_text("STRIPE_KEY=sk_live_Canary123456789012345\n")
    simple = {"private": {"hr": str(t / "hr"), "keys": str(t / "keys")}, "workspace": str(t / "ws"),
              "deadline": 0.3, "job_timeout": 1, "models": {
                  "pop": {"url": "http://x/v1", "model": "local", "sees": "everything", "dir": str(t / "pop")},
                  "cloud": {"api": "anthropic", "model": "cloud", "sees": {"hr": "scrubbed", "keys": "nothing"}}}}
    return t, door.expand(simple, t / "state")


def leaked(text):
    return [i for i in IDS if i.lower() in text.lower()]


def test_forward_cloud_in_front():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        d = door.Door(p, t / "state", "cloud")
        d.prepare(lambda scope: lambda text: [])
        r = d.read(str(t / "hr/people.csv"))
        assert r["row"] == "scrubbed" and not leaked(r["text"]) and "162000" in r["text"], r
        assert "refused" in d.rpc({"method": "tools/call", "id": 1, "params": {"name": "read", "arguments": {
            "path": str(t / "keys/prod.env")}}})["result"]["content"][0]["text"]
        GOT.clear()
        r = d.offload("average salary?", [str(t / "hr/people.csv")])          # on the view: full reply
        assert r["row"] == "full" and not leaked(r["output"]) and not leaked(GOT[-1][1]), r
        r = d.offload("live key?", [str(t / "keys/prod.env")], {"bool": True})   # on raw keys: shaped
        assert r["row"] == "shaped" and r["answer"] is True and r["allowance_left"]["keys"] == 14, r
        assert d.offload("dump it", [str(t / "keys/prod.env")])["row"] == "receipt"
        r = d.offload("live key?", [str(t / "keys")], {"bool": True})             # a directory: all its files
        assert r["answer"] is True and "sk_live_" in GOT[-1][1], r
        os.symlink(t / "keys/prod.env", t / "ws/link.env")                    # a planted link keeps its label
        assert d.item("link.env").scopes == {"keys"}
        for j in d.jobs:
            j.join()


def test_reverse_local_in_front():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        d = door.Door(p, t / "state", "pop")
        d.prepare(lambda scope: lambda text: [])
        assert d.offload("hello", [], node="cloud")["row"] == "full"        # nothing read yet: plain text
        r = d.read(str(t / "hr/people.csv"))
        assert r["row"] == "full" and "Zorbina Quellwater" in r["text"]
        (t / "ws/draft.md").write_text("Zorbina Quellwater earns 162000; mail zq@corp.example")
        GOT.clear()
        r = d.offload("Polish this note for Quillon Brimstoke.", ["draft.md", str(t / "hr/people.csv")], node="cloud")
        assert r["row"] == "full", r
        assert not leaked(GOT[-1][1]) and "[PERSON_" in GOT[-1][1], GOT[-1]  # the cloud got placeholders
        assert "Quillon Brimstoke" in r["output"] and "Zorbina Quellwater" in r["output"]   # the caller real names
        d.read(str(t / "keys/prod.env"))                                     # now its context holds keys
        assert "refused" in d.rpc({"method": "tools/call", "id": 2, "params": {"name": "offload", "arguments": {
            "task": "anything", "node": "cloud"}}})["result"]["content"][0]["text"]


def test_levels():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        assert set(p["nodes"]["cloud"]["trust"]) == {"hr~"} and set(p["nodes"]["pop"]["trust"]) == {"hr", "hr~", "keys", "keys~"}
        try:
            door.expand({"private": {"hr": "/x"}, "models": {"m": {"sees": "a bit"}}}, t)
            raise AssertionError("bad level accepted")
        except door.core.Refused:
            pass


def refusal(d, tool, **args):
    return d.rpc({"method": "tools/call", "id": 9, "params": {"name": tool, "arguments": args}})["result"]["content"][0]["text"]


def test_found_leaks_stay_fixed():
    """Each case is a leak the review found: it must stay closed."""
    import lego
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        d = door.Door(p, t / "state", "cloud")
        d.prepare(lambda scope: lambda text: [])
        d.read(str(t / "hr/people.csv"))
        for probe in ("/[PERSON_1]", "[PERSON_2]:/x", "[EMAIL_1]"):          # refusals never unscrub
            assert not leaked(refusal(d, "read", path=probe)), probe
        (t / "ws/a.txt").write_text("harmless")                              # swap a labelled file for a link
        it = d.item("a.txt")
        os.remove(t / "ws/a.txt")
        os.symlink(t / "keys/prod.env", t / "ws/a.txt")
        try:
            d.bytes_of(it, False)
            raise AssertionError("read through a swapped link")
        except (door.core.Refused, OSError):
            pass
        assert d.vault.unscrub("[PERSON_1]", set()) == "[PERSON_1]"            # no scope, no real name
        for ws in (t / "hr", t / "state", t):                                 # the room may not reach private data
            try:
                door.Door({**p, "workspace": str(ws)}, t / "state", "cloud")
                raise AssertionError(f"workspace {ws} accepted")
            except door.core.Refused:
                pass
        rev = door.Door(p, t / "state", "pop")                                # reverse: read raw, write to ws
        rev.read(str(t / "hr/people.csv"))
        later = door.Door(p, t / "state", "cloud")                            # a later session on that workspace
        assert not later.context <= later.seen                               # lego.session refuses to start it
        (t / "ws2").mkdir()
        fresh = door.Door({**p, "workspace": str(t / "ws2")}, t / "state2", "pop")
        fresh.prepare(lambda scope: lambda text: [])
        GOT.clear()
        fresh.offload("Write to Zorbina Quellwater", [], node="cloud")        # names typed from the listing
        assert not leaked(GOT[-1][1]), GOT[-1]
        assert lego.caller(p, "codex") is None and lego.caller(p, "claude") == "cloud"


if __name__ == "__main__":
    for f in (test_levels, test_forward_cloud_in_front, test_reverse_local_in_front, test_found_leaks_stay_fixed):
        f()
        print("ok  ", f.__name__)
