"""The exchange rule, both ways, with a stub model (no network): what each model is given is what
its level allows, scrubbed text holds no identifier, a model that sees everything gets real names
back, and links or workspace copies do not launder a label."""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LEGO_TAINT"] = tempfile.mkdtemp() + "/taint.json"           # not the user's
import door  # noqa: E402
door.measured = lambda node: False                          # stub nodes: no real disk to measure
import transport  # noqa: E402
import core  # noqa: E402

GOT = []                                                       # (node, what the model was given)


NODE = {}
exec(transport.OPEN, NODE)                                                     # the node's own reader: no link followed


def fake_job(node, files, task, returns, out, keep, timeout, salt=None, **_):     # the files are staged on this host
    seen = task + "".join(Path(f).name + b.decode() for p in files for f, b in NODE["regular"](p))
    GOT.append((node["model"]["model"], seen))
    reply = json.dumps({"answer": True}) if returns else "echo: " + seen
    return {"took": 0.0, "output": None if keep else reply}


real_run = transport.run_job
transport.run_job = fake_job
CSV = "name,email,salary\nZorbina Quellwater,zq@corp.example,162000\nQuillon Brimstoke,qb@corp.example,118000\n"
IDS = ["Zorbina", "Quellwater", "Quillon", "Brimstoke", "zq@corp.example", "qb@corp.example", "sk_live_Canary123456789012345",
       "Varnella", "Oxtrude", "Pellamy", "Strand"]


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


def test_second_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/zq@corp.example.txt").write_text("notes")                    # identifiers in file names only
        (t / "hr/Varnella_Oxtrude.md").write_text("notes")
        rev = door.Door(p, t / "state", "pop")
        rev.prepare(lambda scope: lambda text: [("PERSON", "Varnella Oxtrude")] if "Varnella" in text else [])
        GOT.clear()
        r = rev.offload("Summarise", [str(t / "hr")], node="cloud")          # never read: replies are tokens
        assert not leaked(GOT[-1][1]), GOT[-1]                               # staged names scrubbed too
        assert "Zorbina Quellwater" in r["output"] and "hr" in rev.context   # unscrubbed, so labelled raw
        cloud = door.Door(p, t / "state", "cloud")
        assert not cloud.context <= cloud.seen                               # a later cloud session is refused
        (t / "ws3").mkdir()
        fwd = door.Door({**p, "workspace": str(t / "ws3")}, t / "state3", "cloud")
        fwd.prepare(lambda scope: lambda text: [("PERSON", "Varnella Oxtrude")] if "Varnella" in text else [])
        assert not leaked(fwd.about()), fwd.about()                          # the catalog the cloud sees
        (t / "hr/late.csv").write_text("name,salary\nPellamy Strand,99000\n")   # arrived after prepare
        fwd2 = door.Door({**p, "workspace": str(t / "ws3")}, t / "state3", "cloud")
        assert "1 files added since lego started" in fwd2.about() and not leaked(fwd2.about())
        for probe in (str(t / "hr/late.csv"), str(t / "hr/Pellamy_Strand.md")):
            assert "refused" in refusal(fwd2, "read", path=probe), probe     # raw names are not accepted


def test_third_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        d = door.Door(p, t / "state", "cloud")
        d.prepare(lambda scope: lambda text: [])
        r = d.offload("names", [str(t / "hr/people.csv")], {"bool": True})   # a result on raw hr
        assert "refused" in refusal(d, "read", path=r["ref"])                # is never handed back scrubbed
        assert refusal(d, "read", path=str(t / "keys/nope")) == refusal(d, "read", path=str(t / "keys/prod.env"))
        pub = dict(p, public=[str(t / "ws/pub.md")])                         # a public file in the workspace
        (t / "ws/pub.md").write_text("x")
        rev = door.Door(pub, t / "state", "pop")
        rev.read(str(t / "hr/people.csv"))
        assert "hr" in rev.item("pub.md").scopes                             # still carries what pop read
        assert "[PERSON_" in rev.vault.scrub("zorbinaQuellwaterSalary and ZorbinaQuellwater", {"hr"})
        assert "no file" in refusal(rev, "offload", task="x", inputs=["missing.md"], node="cloud")
        for j in d.jobs:
            j.join()


def test_fourth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        d = door.Door(p, t / "state", "cloud")
        d.prepare(lambda scope: lambda text: [])
        d.read(str(t / "hr/people.csv"))
        GOT.clear()
        guesses = "Zorbina Quellwater, Alice Smith, zq@corp.example, x@corp.example"
        d.offload(guesses, [], node="cloud")                                  # the vault is no oracle
        assert GOT[-1][1] == guesses, GOT[-1]
        (t / "Data").mkdir()                                                  # one directory, two spellings
        os.rename(t / "hr", t / "Data/hr")
        q = door.expand({"private": {"hr": str(t / "Data/hr")}, "workspace": str(t / "data"), "models": {
            "cloud": {"api": "anthropic", "model": "c", "sees": "nothing"}}}, t / "s2")
        try:
            door.Door(q, t / "s2", "cloud")
            raise AssertionError("a case-variant workspace around hr was accepted")
        except door.core.Refused:
            pass
        (t / "ws/sub").mkdir()
        door.Door({**p, "workspace": str(t / "ws/sub")}, t / "s3", "pop").taint({"hr"})   # nested workspaces
        assert "hr" in door.Door(p, t / "s4", "cloud").context
        v = d.vault
        v.learn("pop:/x/a.csv", b"name\nJos\xc3\xa9 N\xc3\xba\xc3\xb1ez\n", ["hr"], also="hr Varnella-Oxtrude.md")
        out = v.scrub("zorbinaQuellwater_salary ZorbinaQuellwater2 quillonbrimstoke Jose Nunez", {"hr"})
        assert not leaked(out) and "Jose" not in out and "Nunez" not in out, out
        for inputs in ("people.csv", '["people.csv"]'):                         # a path or a list sent as text
            assert "must be" not in refusal(d, "offload", task="x", inputs=inputs)


def test_fifth_hunt_stays_fixed():
    import http.server
    import threading
    import time
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/ops.txt").write_text("ops@corp.example 415-555-0134 41 Larkspur Mews\n")
        rev = door.Door(p, t / "state", "pop")                               # nothing read yet
        rev.prepare(lambda scope: lambda text: [])
        (t / "ws/zorbina_quellwater_letter.md").write_text("Dear Zorbina Quellwater, call 4155550134 at Larkspur Mews")
        GOT.clear()
        rev.offload("Polish", ["zorbina_quellwater_letter.md"], node="cloud")   # what pop typed is scrubbed too
        assert not leaked(GOT[-1][1]) and "4155550134" not in GOT[-1][1] and "Larkspur" not in GOT[-1][1], GOT[-1]
        assert not door.Door(p, t / "state", "cloud").held <= rev.nodes["cloud"].trust   # later cloud refused
        r = rev.offload("Write to Zorbina Quellwater", [], node="pop")         # a raw task's output
        assert "refused" in refusal(rev, "offload", task="polish", inputs=[r["ref"]], node="cloud")
        v = rev.vault                                                         # re-tagging refreshes the matcher
        v.scrub("warm the hr matcher", {"hr"})
        assert "ops@corp" not in v.scrub("mail ops@corp.example", {"hr"})
        try:                                                                  # a local node: df would show its jobs
            door.measured = lambda node: True
            door.Door(p, t / "s5", "cloud")
            raise AssertionError("a local node on the workspace disk was accepted")
        except door.core.Refused:
            pass
        finally:
            door.measured = lambda node: False

    class Stall(http.server.BaseHTTPRequestHandler):                          # a model that never answers
        def do_POST(self):
            time.sleep(8)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stall)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    node = {"model": {"model": "m", "base_url": f"http://127.0.0.1:{srv.server_port}/v1"}}
    t0 = time.time()
    try:
        real_run(node, [], "x", None, tmp_out(), False, 3.0)
    except Exception:
        pass
    assert time.time() - t0 < 3.0, time.time() - t0                       # over before the bound, not after
    srv.shutdown()


def test_sixth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/people.csv").write_text(CSV.replace("162000", "162000,415-555-0134,1985-03-14").replace(
            "name,email,salary", "name,email,salary,phone,dob").replace("118000", "118000,415-555-9999,1990-01-02"))
        (t / "hr/Mei_Ng.md").write_text("Mei Ng: fine")
        (t / "hr/zz_Quillon.md").write_text("Quillon asked")
        (t / "keys/owners.csv").write_text("owner\nQuillon Strand\n")     # shares a first name with hr
        d = door.Door(p, t / "state", "cloud")
        d.prepare(lambda scope: lambda text: [("PERSON", "Mei Ng")] if "Mei" in text else [])
        assert not leaked(d.read(str(t / "hr/people.csv"))["text"])
        text = d.vault.scrub("Quillon wrote 4155550134 162000, born 14/03/1985; MeiNg_notes", {"hr"})
        assert not leaked(text) and "4155550134" not in text and "14/03/1985" not in text and "MeiNg" not in text, text
        names = [ln.strip() for ln in d.about().splitlines() if "/hr/" in ln and not ln.strip().startswith(str(t))]
        assert names == sorted(names) and not leaked("".join(names)), names   # shown order, nothing raw
        assert all(len(k.split("_")[-1]) >= 5 for k in d.vault.v["real"])    # [EMAIL_7f3a]: no count of other scopes
        os.makedirs(str(t / "ws/g1/x") + "\n" + str(t) + "/hr")                        # a forged find line
        Path(str(t / "ws/g1/x") + "\n" + str(t) + "/hr/people.csv").write_text("x")
        (t / "ws/g1/x").write_text("x")
        assert all("hr" not in i.scopes for i in d.items(["g1"]))


def test_seventh_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "keys/globex-acquisition-wire-instructions.env").write_text("x")
        (t / "hr/people.csv").write_text("name,phone,dob,employee id\nFrank Brown,415-555-0134,1985-03-04,E-10234\n"
                                         "Siobhan O'Brien,020 7946 0958,1979-11-23,E-20000\n")
        (t / "hr/415_555_0134_call.md").write_text("call")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        listing = pop.read(".")["text"]
        assert "globex" not in listing and "prod.env" not in listing, listing   # keys: the cloud sees nothing
        assert any(i.ref.endswith("prod.env") for i in pop.items([ln.strip() for ln in listing.splitlines() if "/#" in ln][:2]))
        tag = next(ln.strip() for ln in listing.splitlines() if "/#" in ln).rsplit("/", 1)[1]
        assert pop.item("keys/" + tag).scopes == {"keys"}                    # the tail of a listed name will do
        text = pop.vault.scrub("frank_brown FrankBrown frank-brown Ms O\u2019Brien born 3/4/1985, id E10234", {"hr"})
        assert not any(w in text for w in ("rank", "Brien", "1985", "10234")), text
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        assert "0134" not in cloud.about()


def test_eighth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "keys/prod.env").write_text("DB_PASSWORD=Summer2024!\n")
        (t / "keys/owners.csv").write_text("owner\nPellamy Strand\n")      # a view of hr is hr's alone:
        (t / "hr/notes.md").write_text("Varnella called from 415 - 555 - 0134 about E 10234. The hallway.\n")
        (t / "hr/ids.csv").write_text("name,phone,employee id\nVarnella Oxtrude,415-555-0134,E-10234\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        GOT.clear()
        pop.offload("Summarise", [str(t / "hr/people.csv")], node="cloud")
        view = GOT[-1][1]
        pop.offload("Check: Zorbina Quellwater, Alice Smith, zq@corp.example, x@corp.example. Summer2024! Winter2023!",
                    [], node="cloud")                                      # guesses the cloud asked pop to relay
        sent = GOT[-1][1]
        assert not leaked(sent) and "Alice Smith" not in sent, sent         # known and unknown look alike,
        assert not set(re.findall(r"\[[A-Z]+_\w+\]", sent)) & set(re.findall(r"\[[A-Z]+_\w+\]", view))   # unlinked
        assert ("Summer2024!" in sent) == ("Winter2023!" in sent)          # keys: alike, known or not, so no
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")   # replacing to confirm a guess
        (t / "ws2").mkdir()
        text = cloud.read(str(t / "hr/notes.md"))["text"]
        assert not leaked(text) and "0134" not in text and "10234" not in text, text   # spaced forms
        assert "hallway" in text                                              # keys holds no 'hall' to replace
        calls = []
        real = transport.files
        main = threading.current_thread()                                      # the job's own thread may look
        transport.files = lambda *a, **k: (threading.current_thread() is main and calls.append(a)) or real(*a, **k)
        try:
            r = cloud.offload("x", [str(t / "keys/clients/globex")])        # hidden: no file system before the reply
            assert r["row"] == "receipt" and not calls, (r, calls)
        finally:
            transport.files = real
        os.symlink("/etc/hosts", t / "keys/out.lnk")                         # named in keys, linked out of it
        assert refusal(cloud, "read", path=str(t / "keys/out.lnk")) == refusal(cloud, "read", path=str(t / "keys/nope"))
        (t / "ws2/g").mkdir()
        os.symlink(t / "hr/people.csv", t / "ws2/g/a")                       # a listed file swapped for a link
        transport.files = lambda *a, **k: [str(t / "ws2/g/a")]
        try:
            assert not [i for i in cloud.items(["g"]) if "hr" in i.scopes]
        finally:
            transport.files = real
        for j in cloud.jobs:
            j.join()


def test_ninth_hunt_stays_fixed():
    def view(keys_owner):
        with tempfile.TemporaryDirectory() as tmp:
            t, p = setup(os.path.realpath(tmp))
            (t / "keys/owners.csv").write_text(f"owner\n{keys_owner}\n")
            (t / "hr/handbook.md").write_text("Staff shall use the east hallway. Payroll runs monthly.\n")
            (t / "hr/ids.csv").write_text("name,dob,employee id\nZorbina Quellwater,1985-03-14,E-10234\n")
            (t / "hr/contacts.txt").write_bytes("Quillon Brimstoke qb@corp.example\n".encode("utf-16"))
            d = door.Door(p, t / "state", "cloud")
            d.prepare(lambda scope: lambda text: [])
            out = d.read(str(t / "hr/handbook.md"))["text"]
            text = d.vault.scrub("ZORBINA zOrBiNa born March 14, 1985 or 14/03/85, staff no. 10234", {"hr"})
            assert not leaked(text) and "1985" not in text and "14/03/85" not in text and "10234" not in text, text
            assert not leaked(d.read(str(t / "hr/contacts.txt"))["text"])     # UTF-16: read as text, then scrubbed
            same = refusal(d, "read", path=str(t / "keys/nope"))
            for probe in ("../keys/prod.env", str(t / "KEYS/prod.env"), str(t / "ws/../keys/prod.env")):
                assert refusal(d, "read", path=probe) == refusal(d, "read", path=probe.replace("prod.env", "nope")) \
                    and "sealed" not in refusal(d, "read", path=probe), probe   # decided from the name alone
            assert "you may not see" in same
            for j in d.jobs:
                j.join()
            return out
    assert view("Pellamy Hall") == view("Orrin Vex")                         # what keys holds does not show in hr


def shape(text):
    """Placeholders renamed by first appearance: two views compared up to the vault's own key."""
    names = {}
    return re.sub(r"\[[A-Z]+_[0-9a-f]+\]", lambda m: names.setdefault(m.group(0), f"<{len(names)}>"), text)


def test_tenth_hunt_stays_fixed():
    def view(keys_rows, keys_first):
        with tempfile.TemporaryDirectory() as tmp:
            t, p = setup(os.path.realpath(tmp))
            if keys_first:
                p["scopes"] = dict(reversed(list(p["scopes"].items())))     # keys learned first
            (t / "keys/owners.csv").write_text("owner,address,account id,email\n" + keys_rows)
            (t / "hr/ids.csv").write_text("name,address,employee id,email,dob,phone\n"
                                          "Quillon Brimstoke,41 Larkspur Mews,E-10234,qb@corp.example,1985-03-14,415-555-0134\n")
            (t / "hr/notes.md").write_text("Quillon came in. Visit at Larkspur Mews. Staff no. 10234. Born 14th of "
                                           "March 1985, call 415,555,0134 or E 102 34. Mail qb@corp.example.\n")
            d = door.Door(p, t / "state", "cloud")
            d.prepare(lambda scope: lambda text: [])
            out = shape(d.read(str(t / "hr/ids.csv"))["text"] + d.read(str(t / "hr/notes.md"))["text"])
            assert not leaked(out) and not re.search(r"Quillon|Larkspur|10234|1985|0134|102 34|qb@", out), out
            pop = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "pop")
            (t / "ws2").mkdir()
            tok = re.findall(r"\[EMAIL_[0-9a-f]+\]", d.read(str(t / "hr/notes.md"))["text"])[0]
            assert pop.vault.unscrub(tok, {"hr"}) == "qb@corp.example"       # hr's own, whatever keys holds
            fresh = {}
            relay = shape(pop.vault.scrub("Is it zorbina, Quillon, 14th of March 1985, 4155550134, E 102 34?", {"hr"}, fresh))
            os.symlink(t / "keys/clients", t / "ws/link")            # a link into keys, which the cloud sees nothing of
            for j in d.jobs:
                j.join()
            return out, relay, json.loads(refusal(d, "offload", task="x", inputs=["link"])).get("row")
    base = view("Orrin Vex,9 Quarry Lane,AC-77821,ov@corp.example\n", False)
    assert "Quillon" not in base[1] and "zorbina" not in base[1] and "1985" not in base[1], base[1]   # by text alone
    for rows in ("Quillon Strand,9 Larkspur Mews,AC-10234,qb@corp.example\n",):
        for first in (False, True):
            assert view(rows, first) == base, (rows, first)        # the hr view, the relay, the link: keys unseen


def test_eleventh_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/notes.md").write_text("Reach Zorbina at zq@corp.example (or zq(at)corp.example, zq [at] corp [dot] example).\n")
        names = ["Abelard Kint", "Hesper Toll", "Orrin Vex", "Pellamy Strand", "Quillon Brimstoke", "Varnella Oxtrude"]
        (t / "hr/reviews").mkdir()
        for n, name in enumerate(names):
            (t / f"hr/reviews/{name.replace(' ', '_')}.md").write_text(f"review number {n}\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        assert "zq" not in cloud.read(str(t / "hr/notes.md"))["text"]          # (at) and [dot] spellings too
        orders = set()
        for _ in range(4):
            GOT.clear()
            pop.offload("Summarise", [str(t / "hr/reviews")], node="cloud")
            orders.add(tuple(re.findall(r"review number (\d)", GOT[-1][1])))
        assert len(orders) > 1, orders                                          # not the real names' order
        pop.read(str(t / "hr/people.csv"))
        GOT.clear()
        pop.offload("Thanks, Strand. ping mei ng and 王伟 about badge e10234; tags [QUELLWATER_1] [ID_4155550134]",
                    [], node="cloud")
        sent = GOT[-1][1]
        assert not re.search(r"Strand|mei|\bng\b|王伟|e10234|QUELLWATER|4155550134", sent), sent
        home = t / "home"
        (home / ".lego").mkdir(parents=True)
        os.environ["LEGO_TAINT"], was = str(home / ".lego/taint.json"), os.environ["LEGO_TAINT"]
        try:                                                                   # the room may not hold the taints
            door.Door({**p, "workspace": str(home)}, t / "state", "cloud")
            assert False, "a workspace holding the taint file was accepted"
        except core.Refused:
            pass
        finally:
            os.environ["LEGO_TAINT"] = was
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_twelfth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name,email,phone,address\nFrank Brown,jdoe1985@gmail.com,415-555-0134,"
                                        "41 Larkspur Mews\nAmber Baker,ab@corp.example,415-555-7777,2 Elm Row\n"
                                        "Rob Baker,rb@corp.example,415-555-8888,3 Elm Row\n")
        (t / "hr/notes.md").write_text("frank wants a raise; brown is on leave. Call 555-0134 or write jdoe1985 at "
                                       "gmail dot com, near Larkspur.\n")
        (t / "keys/ssh").mkdir()
        os.symlink(t / "hr/people.csv", t / "keys/staff.csv")              # a link inside keys, into hr
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/notes.md"))["text"]
        assert not re.search(r"frank|brown|0134|jdoe1985|Larkspur", view), view
        os.symlink(t / "keys", t / "ws2/k")
        alt = str(t).replace("/private/var/", "/var/") if str(t).startswith("/private/var/") else str(t)
        same = {json.loads(refusal(cloud, "read", path=q)).get("refused", "").split(" ")[0] + str("you may not see" in refusal(cloud, "read", path=q))
                for q in (str(t / "keys/staff.csv"), str(t / "keys/nope"), alt + "/keys/staff.csv", alt + "/keys/nope",
                          "k/staff.csv", "k/nope")}
        assert all(x.endswith("True") for x in same), same                 # one answer, from the name alone
        os.mkfifo(t / "ws2/probe")                                          # a FIFO is refused at once
        assert "regular file" in refusal(cloud, "offload", task="x", inputs=["probe"])
        GOT.clear()                                                         # a name lego gave back to pop, relayed
        pop.offload("Repeat the name of the person you see.", [str(t / "hr/staff.csv")], node="cloud")
        for j in pop.jobs:
            j.join()
        back = pop.vault.unscrub(re.findall(r"\[PERSON_[0-9a-f]+\]", GOT[-1][1])[0], {"hr"}, pop.fresh)
        GOT.clear()
        pop.offload(f"{back} is fine. Baker agrees.", [], node="cloud")
        assert back not in GOT[-1][1] and "Baker" not in GOT[-1][1], GOT[-1][1]
        os.environ["LEGO_TAINT"], was = str(t / "taint2.json"), os.environ["LEGO_TAINT"]
        try:                                                                # 'hr' in another policy is other data
            other = {**p, "scopes": {d.replace("/hr", "/hr2"): s for d, s in p["scopes"].items()}}
            (t / "hr2").mkdir()
            pop2 = door.Door({**p, "workspace": str(t / "ws3")}, t / "state", "pop")
            (t / "ws3").mkdir()
            pop2.taint({"hr"})
            assert door.Door({**other, "workspace": str(t / "ws3")}, t / "state2", "cloud").held - {"hr", "hr~"}
        finally:
            os.environ["LEGO_TAINT"] = was
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_thirteenth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/ids.csv").write_text("name,employee id,phone\nZorbina Quellwater,E-10234,415-555-0134\n")
        (t / "hr/tags.md").write_text("Ticket [EMP_10234] for [QUELLWATER_1], call [TEL_4155550134].\n")
        (t / "sealed.txt").write_text("x")
        (t / "ws/notes.txt").write_text("WS-NOTES")
        for name, target in (("out.env", t / "sealed.txt"), ("staff.csv", t / "hr/people.csv"),
                             ("mirror", t / "hr"), ("cur.env", t / "ws/notes.txt"), ("null.env", "/dev/null")):
            os.symlink(target, t / "keys" / name)                  # links inside keys, each somewhere else
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/tags.md"))["text"]
        assert not re.search(r"10234|QUELLWATER|0134", view), view           # placeholder-shaped text is text
        answers = set()
        for name in ("out.env", "staff.csv", "mirror", "cur.env", "null.env", "nope"):
            os.symlink(t / "keys" / name, t / f"ws2/probe_{name}")
            answers.add(re.sub(r"probe_\S+", "", refusal(cloud, "read", path=f"probe_{name}")))
            answers.add(json.loads(refusal(cloud, "offload", task="x", inputs=[f"probe_{name}"])).get("row"))
        assert len(answers) == 2, answers                                       # one refusal, one receipt, for all
        for j in cloud.jobs:
            j.join()
        def relay(echo):
            d = door.Door({**p, "workspace": str(t / f"ws_{echo}")}, t / f"state_{echo}", "pop")
            (t / f"ws_{echo}").mkdir()
            d.prepare(lambda scope: lambda text: [])
            if echo:                                                            # the cloud echoed a placeholder
                tok = d.vault.scrub("Zorbina Quellwater 415-555-0134", {"hr"})
                d.vault.unscrub(tok, {"hr"}, d.fresh)
            d.read(str(t / "hr/people.csv"))
            GOT.clear()
            d.offload("Guesses: zorbina quellwater, orrin vex, 415-555-0134, 415-555-9999.", [], node="cloud")
            for j in d.jobs:
                j.join()
            return shape(re.sub(r"\[([A-Z]+)_[0-9a-f]+\]", r"[\1_0]", GOT[-1][1]))
        assert relay(True) == relay(False)                                      # the same, whatever lego turned back
        (t / "hr/low.md").write_text("frank wants a raise; brown is on leave.\n")
        (t / "hr/people.csv").write_text((t / "hr/people.csv").read_text() + "Frank Brown,fb@corp.example,1,2,3\n")
        d = door.Door({**p, "workspace": str(t / "ws_copy")}, t / "state_copy", "pop")
        (t / "ws_copy").mkdir()
        d.prepare(lambda scope: lambda text: [])
        (t / "ws_copy/low.md").write_bytes((t / "hr/low.md").read_bytes())   # pop copies an hr file whole
        d.read(str(t / "hr/low.md"))
        GOT.clear()
        d.offload("Summarise", ["low.md"], node="cloud")
        for j in d.jobs:
            j.join()
        assert "frank" not in GOT[-1][1] and "brown" not in GOT[-1][1], GOT[-1][1]   # as the vault scrubs the original
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_fifteenth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name,phone\nJosé Núñez,415-555-0134\nWill Okonkwo,415-555-2222\n")
        (t / "hr/page.html").write_text("<p>Jos&eacute; N&uacute;&ntilde;ez</p>\n")
        (t / "hr/log.txt").write_text("GET /p?user=Jos%C3%A9%20N%C3%BA%C3%B1ez\n")
        (t / "hr/calls.md").write_text("Ping https://wa.me/14155550134 tonight.\n")
        (t / "sealed.txt").write_text("x")
        os.symlink(t / "sealed.txt", t / "keys/out.env")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        for f in ("page.html", "log.txt"):                                     # encoded names: refused, not sent
            assert "encoded" in refusal(cloud, "read", path=str(t / f"hr/{f}")), f
        assert "0134" not in cloud.read(str(t / "hr/calls.md"))["text"]        # with its country code
        for f in ("out.env", "prod.env", "nope.env"):
            os.symlink(f"{t}/keys/./{f}", t / f"ws2/dot-{f}")
        assert len({refusal(cloud, "read", path=f"dot-{f}") for f in ("out.env", "prod.env", "nope.env")}) == 1   # by name
        ns = {}                                                               # the node's runner skips links
        exec(transport.OPEN, ns)
        walk = lambda p: [f for f, _ in ns["regular"](p)]  # noqa: E731
        assert walk(str(t / "keys/out.env")) == [] and str(t / "keys/out.env") not in walk(str(t / "keys"))
        pop.read(str(t / "hr/staff.csv"))
        GOT.clear()
        pop.offload("Will asked for leave. May covers. frank agrees.", [], node="cloud")
        assert not re.search(r"Will|May|frank", GOT[-1][1]), GOT[-1][1]        # first names, any case
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_sixteenth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name\nJosé Núñez\nMei Ng\n")
        (t / "hr/review.rtf").write_text("{\\rtf1 Review for Jos\\'e9 N\\'fa\\'f1ez: exceeds.}\n")
        (t / "hr/msg.eml").write_text("From: =?UTF-8?B?Sm9zw6kgTsO6w7Fleg==?=\nTo: TWVpIE5n\n")
        (t / "hr/app.log").write_text("login user=b'Jos\\xc3\\xa9 N\\xc3\\xba\\xc3\\xb1ez'\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        for f in ("review.rtf", "msg.eml", "app.log"):
            assert "encoded" in refusal(cloud, "read", path=str(t / f"hr/{f}")), f
        seen = cloud.read(str(t / "hr/people.csv"))["text"]                  # session under policy A
        (t / "hr2").mkdir()
        (t / "hr2/x.csv").write_text("name\nOrrin Vex\n")
        other = {**p, "scopes": {d.replace(str(t / "hr"), str(t / "hr2")): s for d, s in p["scopes"].items()},
                 "workspace": str(t / "ws3")}
        (t / "ws3").mkdir()
        b = door.Door(other, t / "state", "pop")                               # policy B: its "hr" is hr2
        b.prepare(lambda scope: lambda text: [])
        back = b.vault.unscrub(seen, {"hr"})
        assert not leaked(back), back                                          # A's placeholders mean nothing here
        for j in pop.jobs + cloud.jobs + b.jobs:
            j.join()


def test_seventeenth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name\nZhang Wei\nMei Zhang\n")
        (t / "hr/glued.md").write_text("ZhangWei, zhangwei and MeiZhang met.\n")
        os.symlink(t / "hr", t / "keys/mirror")                               # the user's own link, keys -> hr
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        cloud.prepare(lambda scope: lambda text: [])
        view = cloud.read(str(t / "hr/glued.md"))["text"]
        assert not re.search(r"(?i)zhang|wei|mei", view), view                  # a short name glued to a long one
        GOT.clear()
        cloud.offload("Is Zorbina in this file?", [str(t / "keys/mirror/people.csv")], returns={"bool": True}, node="pop")
        for j in cloud.jobs:
            j.join()
        assert not any(leaked(g.removeprefix("Is Zorbina in this file?")) for _, g in GOT), GOT   # keys only: the
                                                                                # link gives the node no file
        walk = transport.files
        transport.files = lambda node, root: walk(node, root) if "keys" not in root else 1 / 0   # keys: never walked
        try:
            assert "1 files" in cloud.read(".")["text"]                          # keys' count, from before the agent
        finally:
            transport.files = walk
        def sent(guess):                                                        # pop relays a guess at a keys file
            d = door.Door({**p, "workspace": str(t / f"ws_{len(guess)}_{guess[-2]}")}, t / "state", "pop")
            Path(d.ws).mkdir()
            (Path(d.ws) / "g.txt").write_text(guess)
            GOT.clear()
            d.offload("Summarise", ["g.txt"], node="cloud")
            for j in d.jobs:
                j.join()
            return shape(re.sub(r"\[([A-Z]+)_[0-9a-f]+\]|[0-9a-f]{16}-", r"[\1_0]", GOT[-1][1]))
        assert sent((t / "keys/prod.env").read_text()) == sent("STRIPE_KEY=sk_live_Wrong9876543210987654321\n")
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_eighteenth_hunt_stays_fixed():
    import scrub
    v = scrub.Vault(tempfile.mkdtemp() + "/v.json")
    for text in ("Manager: List", "- List: raise? No objections.", "Prof. List signs.", "Ask F. List.",
                 "J. Read starts.", "Sign-off: Prof. Good.", "Ask Capt. Read."):
        out = v.own(scrub.norm(text), {}, {"hr"})
        assert not re.search(r"\b(List|No|Read|Good)\b", out), (text, out)   # names after a title or initial
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name\nWei Li\nMei Ng\n")
        (t / "hr/logins.md").write_text("liwei, weili, LIWEI, ngmei, NGMEI, meing\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/logins.md"))["text"]
        assert not re.search(r"(?i)li|wei|mei|ng", view), view                  # short parts glued, any order
        for public, inner in (([], {str(t / "hr/keys"): "keys"}), ([str(t / "pub")], {str(t / "pub/hr"): "hr"})):
            try:
                q = {**p, "public": public, "scopes": {**p["scopes"], **{f"{pop.host}:{d}": s for d, s in inner.items()}}}
                door.Door(q, t / "state_n", "pop")
                raise AssertionError("nested scope accepted")
            except core.Refused as e:
                assert "inside" in str(e), e
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_nineteenth_hunt_stays_fixed():
    import scrub
    v = scrub.Vault(tempfile.mkdtemp() + "/v.json")
    for text in ("Det. Read signs.", "Sen. Good agrees.", "Mme. Best", "dr. Read", "prof. Good"):
        out = v.own(scrub.norm(text), {}, {"hr"})
        assert not re.search(r"\b(Read|Good|Best)\b", out), (text, out)      # a surname after any title
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name,phone\nWei Li,415-555-0134\nMei Ng,1\nZhang Wei,2\n")
        (t / "hr/logins.md").write_text("liweiAdmin, ngmeiDev, weiliOps, zhangweiops, MEINGadmin, adminliwei\n")
        (t / "keys/notes.md").write_text("vendor line 5550134, rotate monthly\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/logins.md"))["text"]
        assert not re.search(r"(?i)li|wei|mei|ng", view), view                  # known parts glued, with a suffix
        before = json.dumps(pop.vault.v["by"].get("keys", {}), sort_keys=True)
        pop.vault.scrub((t / "hr/staff.csv").read_text(), {"hr", "keys"})       # a copy scrubbed for both scopes
        assert json.dumps(pop.vault.v["by"].get("keys", {}), sort_keys=True) == before   # keys' table learns no hr
        listings = []
        for target in ("keys/sub", "keys/none"):                              # a link into keys, found or not
            if target == "keys/sub":
                (t / target).mkdir()
            os.symlink(t / target, t / "ws2/probe")
            listings.append(cloud.read(".")["text"])
            os.unlink(t / "ws2/probe")
        assert listings[0] == listings[1], listings
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_twentieth_hunt_stays_fixed():
    import scrub
    v = scrub.Vault(tempfile.mkdtemp() + "/v.json")
    out = v.own(scrub.norm("Draft to Dña. Read and DDr. Best, MSc. Good."), {}, {"hr"})
    assert not re.search(r"\b(Read|Best|Good)\b", out), out                   # any short abbreviation
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name\nWei Li\nJin Kim\n")
        (t / "hr/logins.md").write_text("adminliweiops wli jkim kimj\n")
        (t / "hr/orrin.vex@corp.example.md").write_text("note\n")
        (t / "outside").mkdir()
        (t / "outside/salaries.csv").write_text("SECRETSAL 162000\n")
        os.symlink(t / "outside/salaries.csv", t / "hr/salaries.csv")       # the user's links
        os.symlink(t / "hr", t / "keys/mirror")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/logins.md"))["text"]
        assert not re.search(r"(?i)li|wei|kim", view), view                    # known parts anywhere, initials
        assert "orrin" not in cloud.read(".")["text"]                           # an email in a file name
        dev = os.stat(t / "keys/prod.env").st_dev
        probes = {refusal(cloud, "read", path=f"/.vol/{dev}/{n}") for n in (os.stat(t / "keys/prod.env").st_ino, 999999999)}
        assert len(probes) == 1, probes                                         # inode paths: never resolved
        real = transport.run_job
        transport.run_job = lambda *a, **k: {"took": 0.0, "output": '{"answer": ' + "[" * 100000 + "]" * 100000 + "}"}
        try:
            r = cloud.offload("Is it live?", [str(t / "keys")], returns={"bool": True}, node="pop")
            assert r.get("row") == "shaped" and r["answer"] == "UNKNOWN", r     # one of the outcomes paid for
        finally:
            transport.run_job = real
        simple = {"private": {"hr": str(t / "hr"), "keys": str(t / "keys")}, "workspace": str(t / "ws3"), "deadline": 0.3,
                  "job_timeout": 1, "models": {
                      "pop": {"url": "http://x/v1", "model": "local", "sees": "everything", "dir": str(t / "pop")},
                      "mid": {"url": "http://y/v1", "model": "mid", "sees": {"hr": "nothing", "keys": "everything"},
                              "dir": str(t / "mid")}}}
        (t / "ws3").mkdir()
        mid = door.Door(door.expand(simple, t / "state3"), t / "state3", "mid")
        mid.prepare(lambda scope: lambda text: [])
        got = json.dumps(mid.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "read", "arguments": {"path": str(t / "keys/mirror/salaries.csv")}}}))
        assert "SECRETSAL" not in got, got                                      # through keys, into hr, out of it
        try:
            door.Door(door.expand({**simple, "private": {"hr": str(t / "hr"), "bak": str(t / "hr~")}}, t / "s4"), t / "s4", "pop")
            raise AssertionError("a directory ending in ~ was accepted")
        except core.Refused:
            pass
        for j in pop.jobs + cloud.jobs + mid.jobs:
            j.join()


def test_twentyfirst_hunt_stays_fixed():
    import scrub
    v = scrub.Vault(tempfile.mkdtemp() + "/v.json")
    out = v.own(scrub.norm('Reviewer "Dr. Best" signs; cc (Mr. Read). **Dr. Best**; (J. Read) [Prof. Good]'), {}, {"hr"})
    assert not re.search(r"\b(Read|Best|Good)\b", out), out                   # a title in brackets or quotes
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        os.symlink(t / "hr", t / "keys/mirror")
        (t / "fin").mkdir()
        simple = {"private": {"hr": str(t / "hr"), "keys": str(t / "keys"), "fin": str(t / "fin")},
                  "workspace": str(t / "ws3"), "deadline": 0.3, "job_timeout": 1, "models": {
                      "pop": {"url": "http://x/v1", "model": "local", "sees": "everything", "dir": str(t / "pop")},
                      "mid": {"url": "http://y/v1", "model": "mid", "dir": str(t / "mid"),
                              "sees": {"hr": "nothing", "keys": "everything", "fin": "everything"}}}}
        (t / "ws3").mkdir()
        mid = door.Door(door.expand(simple, t / "state3"), t / "state3", "mid")
        mid.prepare(lambda scope: lambda text: [])
        hr = str(t / "hr")
        def blind(f):                                                           # no look inside hr, of any kind
            return lambda q, *a, **k: (1 / 0) if str(q).startswith(hr) else f(q, *a, **k)
        canon, real = door.canon, os.path.realpath
        door.canon, os.path.realpath = blind(canon), blind(real)
        def ask(world):
            if world == "link":
                os.symlink(t / "fin", t / "hr/sub")
            elif world == "dir":
                (t / "hr/sub").mkdir()
            try:
                return [json.dumps(mid.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "read", "arguments": {"path": str(t / "keys/mirror" / q)}}})) for q in ("sub/x", "sub" + "/a" * 60)]
            finally:
                os.unlink(t / "hr/sub") if world == "link" else os.rmdir(t / "hr/sub") if world == "dir" else None
        try:
            assert ask("link") == ask("dir") == ask("none")                     # hr's links and names: not shown
        finally:
            door.canon, os.path.realpath = canon, real
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2/sub").mkdir(parents=True)
        (t / "ws2/sub/f").write_text("x")
        os.mkfifo(t / "keys/deploy.fifo")
        walk, rows = transport.files, []
        for target in ("prod.env", "deploy.fifo"):
            def swap(node, root, target=target):                                # the agent swaps a link in mid-walk
                found = walk(node, root)
                if root.endswith("/sub"):
                    os.unlink(t / "ws2/sub/f")
                    os.symlink(t / "keys" / target, t / "ws2/sub/f")
                return found
            transport.files = swap
            try:
                rows.append(json.loads(refusal(cloud, "offload", task="x", inputs=["sub"])).get("row"))
            finally:
                transport.files = walk
                os.unlink(t / "ws2/sub/f")
                (t / "ws2/sub/f").write_text("x")
        assert rows[0] == rows[1], rows                                         # a FIFO in keys looks like a file
        for j in mid.jobs + cloud.jobs:
            j.join()


def test_twentysecond_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/staff.csv").write_text("name\nSean O'Brien\nDina D'Angelo\n")
        (t / "hr/notes.md").write_text("O'brien, o'Brien, O Brien, sobrien and d'angelo met.\n")
        (t / "hr/review.md").write_text("Review: Zorbina Quellwater exceeded every target.\n")
        (t / "fin").mkdir()
        os.symlink(t / "hr", t / "keys/mirror")
        os.symlink(t, t / "keys/up")                                           # a link to the folder holding hr
        os.mkfifo(t / "keys/deploy.fifo")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/notes.md"))["text"]
        assert not re.search(r"(?i)brien|angelo", view), view                  # apostrophe names, any spelling
        up = cloud.upto
        answers = set()
        for target in ("keys/prod.env", "keys/nope.env", "keys/deploy.fifo", "keys/mirror/people.csv", "keys"):
            (t / "ws2/x").write_text("plain")
            def swap(path, target=target):                                      # swapped in just after naming
                got = up(path)
                os.unlink(t / "ws2/x")
                os.symlink(t / target, t / "ws2/x")
                return got
            cloud.upto = swap
            answers.add(refusal(cloud, "read", path="x"))
            os.unlink(t / "ws2/x")
        cloud.upto = up
        assert len(answers) == 1, answers                                       # the same, wherever it points
        try:
            pop.offload("Who earns more?", [str(t / "hr/staff.csv")], returns={"enum": ["Zorbina Quellwater", "other"]},
                        node="cloud")
            raise AssertionError("a real name in returns crossed")
        except core.Refused:
            pass
        def sent(guess):                                                        # the cloud's guesses, relayed
            d = door.Door({**p, "workspace": str(t / f"ws_{guess}")}, t / "state", "pop")
            Path(d.ws).mkdir()
            (Path(d.ws) / "draft.md").write_text(f"Review: {guess} exceeded every target.\n")
            GOT.clear()
            d.offload("Polish", ["draft.md"], node="cloud")
            for j in d.jobs:
                j.join()
            return shape(re.sub(r"\[([A-Z]+)_[0-9a-f]+\]|[0-9a-f]{16}-", r"[\1_0]", GOT[-1][1]))
        assert sent("Zorbina Quellwater") == sent("Orrin Vexley")             # the same whichever is right
        simple = {"private": {"hr": str(t / "hr"), "keys": str(t / "keys"), "fin": str(t / "fin")},
                  "workspace": str(t / "ws3"), "deadline": 0.3, "job_timeout": 1, "models": {
                      "pop": {"url": "http://x/v1", "model": "local", "sees": "everything", "dir": str(t / "pop")},
                      "mid": {"url": "http://y/v1", "model": "mid", "dir": str(t / "mid"),
                              "sees": {"hr": "nothing", "keys": "everything", "fin": "scrubbed"}}}}
        (t / "ws3").mkdir()
        mid = door.Door(door.expand(simple, t / "state3"), t / "state3", "mid")
        mid.prepare(lambda scope: lambda text: [])
        walk = transport.files
        transport.files = lambda node, root: (1 / 0) if root.startswith(str(t / "hr")) or root == str(t) else walk(node, root)
        try:
            for q in ("keys/up", "keys/up/hr/people.csv", "keys/mirror/x", "keys/mirror/people.csv"):
                assert "link lego does not follow" in refusal(mid, "offload", task="x", inputs=[str(t / q)],
                                                              returns={"bool": True}), q   # never walked or labelled
        finally:
            transport.files = walk
        for j in pop.jobs + cloud.jobs + mid.jobs:
            j.join()


def test_twentythird_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        for d in ("box/hr", "box/keys", "archive-of-human-resources"):
            (t / d).mkdir(parents=True)
        (t / "box/hr/people.csv").write_text(CSV)
        (t / "box/keys/prod.env").write_text("x\n")
        os.symlink(t / "box/hr", t / "box/keys/mirror")                       # the user's link, on another node
        node = lambda trust, n: {"trust": trust, "out": str(t / n / "out"), "stage": str(t / n / "stage")}  # noqa: E731
        q = {**p, "scopes": {f"box:{t}/box/hr": "hr", f"box:{t}/box/keys": "keys", **p["scopes"]},
             "nodes": {**p["nodes"], "box": node(["hr", "hr~", "keys", "keys~"], "box"),
                       "mid": {**node(["keys", "keys~"], "mid"), "model": {"url": "http://mid/v1", "model": "mid",
                                                                         "base_url": "http://mid/v1"}}}}
        mid = door.Door({**q, "workspace": str(t / "ws3")}, t / "state3", "mid")
        (t / "ws3").mkdir()
        mid.prepare(lambda scope: lambda text: [])
        got = json.dumps(mid.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "read", "arguments": {"path": f"box:{t}/box/keys/mirror/people.csv"}}}))
        assert not leaked(got), got                                             # no link followed on any node
        assert "another machine" in refusal(mid, "offload", task="x", inputs=[f"box:{t}/box/hr"], returns={"bool": True},
                                            node="pop")                         # never packed from this disk
        (t / "hr/orrin.vexley@corp.example.md").write_text("note\n")
        two = {**p, "scopes": {f"here:{t}/archive-of-human-resources": "hr", **p["scopes"]}}
        pop = door.Door(two, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**two, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        os.symlink(t / "keys", t / "ws2/z")
        scan = os.scandir
        os.scandir = lambda d: scan(d) if isinstance(d, int) else 1 / 0          # folders entered by descriptor only
        try:
            listing = cloud.read(".")["text"]
        finally:
            os.scandir = scan
        assert "orrin" not in listing and "prod.env" not in listing and "  z\n" in listing, listing
        for j in mid.jobs + pop.jobs + cloud.jobs:
            j.join()


def test_twentyfourth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/intl.csv").write_text("name\nŁukasz Wróbel\nSøren Østergaard\nFẹ́mi Adébáyọ̀\nMehmet Yılmaz\n")
        (t / "hr/leave.md").write_text("Lukasz Wrobel, Soren Ostergaard, Soeren, Femi Adebayo and Mehmet Yilmaz.\n")
        (t / "hr/mehmet_yilmaz-leave.md").write_text("x\n")
        for d in ("box/hr", "box/keys/big", "pop/out"):
            (t / d).mkdir(parents=True)
        (t / "box/hr/people.csv").write_text(CSV)
        (t / "box/keys/big/a.txt").write_text("x\n")
        os.symlink(t / "box/hr", t / "box/keys/mirror")
        os.symlink("/etc/hosts", t / "box/hr/lnk")
        os.mkfifo(t / "box/hr/pipe")
        (t / "pop/out/real.txt").write_text("done\n")                         # a job's output, on this disk
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/leave.md"))["text"]
        assert not re.search(r"(?i)lukasz|wrobel|soe?ren|ostergaard|femi|adebayo|yilmaz", view), view
        assert "yilmaz" not in cloud.read(".")["text"].lower()                # and the catalog still answers
        probe = lambda q: refusal(cloud, "read", path=q).replace(q, "<p>")  # noqa: E731
        assert len({probe(f"{t}/pop/out/{f}") for f in ("real.txt/x", "miss.txt/x", "real.txt/x/y", "real.txt",
                                                        "real.txt.trace.json")}) == 1   # sealed: never looked at
        node = lambda trust, n, **m: {"trust": trust, "out": str(t / n / "out"), "stage": str(t / n / "stage"), **m}  # noqa: E731
        model = lambda u: {"model": {"url": u, "model": "m", "base_url": u}}  # noqa: E731
        q = {**p, "scopes": {f"Box:{t}/box/hr": "hr", f"Box:{t}/box/keys": "keys", **p["scopes"]},
             "nodes": {**p["nodes"], "Box": node(["hr", "hr~", "keys", "keys~"], "Box", **model("http://box/v1")),
                       "mid": node(["keys", "keys~"], "mid", **model("http://mid/v1"))}}
        mid = door.Door({**q, "workspace": str(t / "ws3")}, t / "state3", "mid")
        (t / "ws3").mkdir()
        mid.prepare(lambda scope: lambda text: [])
        hidden = {refusal(mid, "read", path=f"Box:{t}/box/hr/{f}").replace(f, "<f>") for f in ("lnk", "pipe", "people.csv")}
        assert len(hidden) == 1, hidden                                         # a capital node name: still hidden
        for q_ in (f"Box:{t}/box/keys/mirror/people.csv", f"Box:{t}/box/keys/mirror"):
            assert "link" in refusal(mid, "offload", task="x", inputs=[q_], node="Box"), q_   # raw on its own node
        ssh = {"ssh": "box"}                                                     # over ssh: the node-side reader
        sh = transport._sh
        transport._sh = lambda n, cmd, data=None, timeout=60: subprocess.run(["/bin/sh", "-c", cmd], input=data,
                                                                            capture_output=True, timeout=timeout).stdout
        try:
            assert transport.files(ssh, f"{t}/box/keys/mirror") == [] and transport.files(ssh, f"{t}/box/keys") == [
                f"{t}/box/keys/big/a.txt"]
            assert transport.read(ssh, f"{t}/box/keys/mirror/people.csv") == b""
        finally:
            transport._sh = sh
        for j in pop.jobs + cloud.jobs + mid.jobs:
            j.join()


def test_twentyfifth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/tr.csv").write_text("name\nEmine Demir\nDana Hayes\nLuis Barnes\n")
        (t / "hr/bordro.md").write_text("EMİNE DEMİR and Agent 47 Quix and Agent-47 Quix.\n")
        (t / "hr/EMİNE DEMİR bordro.txt").write_text("x\n")
        for d in ("box/hr/sub", "ws2"):
            (t / d).mkdir(parents=True)
        (t / "ws2/ok.txt").write_text("ok\n")
        out = Path(p["nodes"]["pop"]["out"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "real.txt").write_text("done\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [("PERSON", "Agent47 Quix")] if scope == "hr" else [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        view = cloud.read(str(t / "hr/bordro.md"))["text"]
        assert not re.search(r"(?i)demir|emi\W?ne|quix|agent", view), view            # no crash, no name
        assert "DEM" not in cloud.read(".")["text"]
        def probe(link, target):                                                 # '..' by name: the same reply
            os.symlink(target, t / "ws2" / link)                                # whatever lies on the way
            return refusal(cloud, "read", path=link)
        assert probe("a", f"{out}/real.txt/../../../ws2/ok.txt") == probe("b", f"{out}/miss.txt/../../../ws2/ok.txt")
        GOT.clear()
        pop.offload("Ask hayes, barnes, huber and mayer; it got deeper.", [], node="cloud")
        assert not re.search(r"hayes|barnes|huber|mayer", GOT[-1][1]) and "deeper" in GOT[-1][1], GOT[-1][1]
        node = lambda trust, n, **m: {"trust": trust, "out": str(t / n / "out"), "stage": str(t / n / "stage"), **m}  # noqa: E731
        q = {**p, "scopes": {f"box:{t}/box/hr": "hr", **p["scopes"]}, "nodes": {**p["nodes"], "box": node(["hr", "hr~"], "box"),
             "mid": node(["keys", "keys~"], "mid", model={"url": "http://mid/v1", "model": "m", "base_url": "http://mid/v1"})}}
        mid = door.Door({**q, "workspace": str(t / "ws3")}, t / "state3", "mid")
        (t / "ws3").mkdir()
        (t / "ws3/ok.txt").write_text("ok\n")
        os.symlink(f"{t}/box/hr/sub/../../../ws3/ok.txt", t / "ws3/a")
        os.symlink(f"{t}/box/hr/none/../../../ws3/ok.txt", t / "ws3/b")
        assert refusal(mid, "read", path="a") == refusal(mid, "read", path="b")    # a local node's hidden scope
        measured = door.measured
        door.measured = door.here                                               # df sees every volume here
        try:
            refusal(door.Door(p, t / "s5", "cloud"), "read", path=".")
            raise AssertionError("a local node running hidden jobs was accepted")
        except core.Refused:
            pass
        finally:
            door.measured = measured
        for j in pop.jobs + cloud.jobs + mid.jobs:
            j.join()


def test_twentysixth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "box/notes").mkdir(parents=True)
        (t / "box/notes/a.txt").write_text("SEALED-CANARY\n")
        (t / "divorce/lawyer.txt").parent.mkdir()
        (t / "divorce/lawyer.txt").write_text("x\n")
        node = lambda trust, n, **m: {"trust": trust, "out": str(t / n / "out"), "stage": str(t / n / "stage"), **m}  # noqa: E731
        q = {**p, "scopes": {f"box:{t}/box/notes": "notes", **p["scopes"]},
             "nodes": {**p["nodes"], "box": node(["notes", "notes~"], "box", ssh="box"),
                       "here": {**p["nodes"]["here"], "trust": p["nodes"]["here"]["trust"] + ["notes", "notes~"]},
                       "mid": node(["notes", "keys", "keys~"], "mid", model={"url": "http://mid/v1", "model": "m",
                                                                           "base_url": "http://mid/v1"})}}
        mid = door.Door({**q, "workspace": str(t / "ws3")}, t / "state3", "mid")
        (t / "ws3").mkdir()
        sh, ran = transport._sh, []
        transport._sh = lambda *a, **k: ran.append(a) or b""
        try:                                                                    # Notes beside notes, on a Linux node:
            assert "is sealed" in refusal(mid, "read", path=f"box:{t}/box/Notes/a.txt")
            assert not any("Notes" in a[1] for a in ran), ran                   # never looked at
        finally:
            transport._sh = sh
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        probe = lambda q: refusal(cloud, "read", path=q).replace(q, "<q>")  # noqa: E731
        assert len({probe(f"{t}/{d}/{c}") for d in ("divorce", "divorce/lawyer.txt", "nope") for c in ("\0", "\ud800", "x")}) == 2
        assert "name files by plain paths" in probe(f"{t}/divorce/\0")      # refused before the disk is looked at
        for bad in ("hr~", "sealed", "a,b"):
            try:
                door.Door({**p, "scopes": {f"here:{t}/hr": bad, f"here:{t}/keys": "keys"}}, t / "s6", "cloud")
                raise AssertionError(f"scope name {bad!r} accepted")
            except core.Refused:
                pass


def test_twentyseventh_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/more.csv").write_text("name\nMinh Tran\nThi Le\nAnne Vance\n")
        (t / "hr/crlf.txt").write_bytes(b"Zorbi-\r\nna Quell-\r\nwater, QUELL-\r\nWATER, Brim-\rstoke, Quell- \nwater\r\n")
        memo = "Payroll: the bank transfer is approved. Please let us know; we planned an advance on the salary channel.\n"
        (t / "hr/memo.txt").write_text(memo)
        (t / "hr/Zorbina.md").write_text("file A\n")
        (t / "hr/Quellwater.md").write_text("file B\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/crlf.txt"))["text"]
        assert not leaked(view), view                                           # any line ending
        assert cloud.read(str(t / "hr/memo.txt"))["text"] == memo               # words stay words: tran+sfer would
        names = [n.strip() for n in cloud.read(".")["text"].splitlines() if n.strip().endswith(".md") or "#" in n]
        assert len(names) == len(set(names)) == 2, names                        # never one name for two files
        assert {cloud.read(n)["text"] for n in names} == {"file A\n", "file B\n"}
        node = lambda n: {"trust": ["notes", "notes~"], "out": str(t / n / "out"), "stage": str(t / n / "stage")}  # noqa: E731
        here = {**p["nodes"]["here"], "trust": p["nodes"]["here"]["trust"] + ["notes", "notes~"]}
        for scopes, extra in (({f"box:{t}/ws9/notes": "notes"}, {"workspace": str(t / "ws9")}),   # a workspace holding it
                              ({f"box:{t}/hr/notes": "notes"}, {}),                              # inside a private dir
                              ({f"box:{t}/pub": "notes"}, {"public": [f"{t}/pub/a.txt"]})):  # holding a public file
            try:
                door.Door({**p, **extra, "scopes": {**p["scopes"], **scopes}, "nodes": {**p["nodes"], "box": node("box"),
                                                                                       "here": here}}, t / "s7", "cloud")
                raise AssertionError(f"accepted {scopes}")
            except core.Refused:
                pass
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_twentyeighth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/more.csv").write_text("name\nMackenzie Brimstoke\nAnna Baker\nOliver Smith\nGrace Hopper\nAnna Good\n")
        (t / "hr/notes.txt").write_text("QuellWater asked; ZorBina too. MacKenzie left. QuellWaterReport. BakerAnna, "
                                         "SmithOliver, #TeamBaker, bAkEr, BAKERS, baker2, smith01, DrBaker, HopperLab, "
                                         "smithFamily. The bank transfer was planned.\n")
        (t / "hr/BakerAnna-review.md").write_text("x\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/notes.txt"))["text"] + cloud.read(".")["text"]
        assert not re.search(r"(?i)quell|zor|kenzie|baker|smith|hopper|oliver|anna", view), view   # in every case
        assert "transfer was planned" in view and "Report" in view               # and words stay words
        pop.read(str(t / "hr/more.csv"))
        GOT.clear()
        pop.offload("Draft replies to Mr./Mrs. Good, Ms./Mr. Good and Prof.Dr. Good.", [], node="cloud")
        assert "Good" not in GOT[-1][1], GOT[-1][1]                               # a joined title: still a title
        for j in pop.jobs + cloud.jobs:
            j.join()


def test_twentyninth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/more.csv").write_text("name\nLena Ritter\nHenning Bonner\n")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        pop.read(str(t / "hr/more.csv"))
        GOT.clear()
        pop.offload("Ask ritter and henning about bonner.", [], node="cloud")
        assert not re.search(r"ritter|henning|bonner", GOT[-1][1]), GOT[-1][1]   # caller text: no doubling rule
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        put, staged = transport.put_tree, []
        transport.put_tree = lambda node, stage, src, timeout=60: staged.append(src) or put(node, stage, src, timeout)
        try:
            for f in ("prod.env", "staging.env", "prod.env"):                  # what crosses: the whole scope, once
                cloud.offload("Is it live?", [str(t / "keys" / f)], node="pop")
            for j in cloud.jobs:
                j.join()
        finally:
            transport.put_tree = put
        assert staged == [str(t / "keys")], staged
        class Model(http.server.BaseHTTPRequestHandler):                        # a model whose output varies
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps({"answer": True}) + " " * (5000 if "long" in body["messages"][1]["content"] else 0)
                data = json.dumps({"choices": [{"message": {"content": out}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sh, sizes = transport._sh, []
        transport._sh = lambda *a, **k: (lambda b: sizes.append(len(b)) or b)(sh(*a, **k))
        try:
            node = {"model": {"model": "m", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1"}}
            for task in ("short", "long"):
                r = real_run(node, [], task, {"bool": True}, str(t / f"out/{task}.txt"), keep=False, timeout=30, shaped=True)
                assert r["answer"] is True and r["output"] is None, r
        finally:
            transport._sh = sh
            srv.shutdown()
        assert sizes[1] == sizes[3], sizes                                       # the reply: one size, whatever it held


def test_thirtieth_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/more.csv").write_text("name\nAnna Brown\nFrank Quellwater\nGrace Hopper\nMei Park\n")
        (t / "hr/access.log").write_text("disabled: abrown, frankq, graceh, mpark. TeamGrace, HopperGrace. The kingdom.\n")
        os.symlink(t / "hr", t / "keys/link")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        view = cloud.read(str(t / "hr/access.log"))["text"]
        assert not re.search(r"(?i)brown|frank|grace|park", view) and "kingdom" in view, view
        for guess in ("prod.env/x", "nope/x", "link/x"):                       # a file, nothing, a link: a job
            GOT.clear()                                                         # runs all the same
            cloud.offload("x", [str(t / "keys" / guess)], node="pop")
            for j in cloud.jobs:
                j.join()
            assert len(GOT) == 1, (guess, GOT)
        class Model(http.server.BaseHTTPRequestHandler):                        # fast, slow or failing
            def do_POST(self):
                task = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][1]["content"]
                time.sleep(1.5 if "slow" in task else 0)
                data = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(500 if "fail" in task else 200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sh, seen = transport._sh, []
        transport._sh = lambda *a, **k: (lambda t0, b: seen.append((len(b), time.time() - t0)) or b)(time.time(), sh(*a, **k))
        try:
            node = {"model": {"model": "m", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1"}}
            for task in ("fast", "slow", "fail"):
                real_run(node, [], task, None, str(t / f"out/{task}.txt"), keep=True, timeout=4)
        finally:
            transport._sh = sh
            srv.shutdown()
        replies = seen[1::2]                                                     # each job: mkdir, then the runner
        assert len({n for n, _ in replies}) == 1, replies                       # one size, at one time, however the
        assert max(s for _, s in replies) - min(s for _, s in replies) < 0.3, replies   # model did


def test_thirty_first_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/Zorbina_Quellwater-review.md").write_text("Review: Zorbina Quellwater exceeded targets.\n")
        (t / "hr/Al_Wu-review.md").write_text("Review: Al Wu exceeded targets.\n")
        simple = {"private": {"hr": str(t / "hr"), "keys": str(t / "keys")}, "workspace": str(t / "ws"),
                  "deadline": 0.3, "job_timeout": 1, "models": {
                      "pop": {"url": "http://x/v1", "model": "local", "sees": "everything", "dir": str(t / "pop")},
                      "mid": {"url": "http://y/v1", "model": "local", "sees": {"hr": "everything", "keys": "nothing"},
                              "dir": str(t / "mid")},
                      "two": {"url": "http://z/v1", "model": "local", "sees": "everything", "dir": str(t / "two")},
                      "cloud": {"api": "anthropic", "model": "cloud", "sees": {"hr": "scrubbed", "keys": "nothing"}}}}
        p = door.expand(simple, t / "state")
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
        (t / "ws2").mkdir()
        cloud.prepare(lambda scope: lambda text: [])
        log, grew = t / "state/door.jsonl", []
        cloud.read(".")
        for alias in [a for a in cloud.alias if "review" in a]:                 # a log line: one size, whatever
            n = log.stat().st_size                                              # name it holds
            cloud.read(alias)
            grew.append(log.stat().st_size - n)
        assert grew == [4096, 4096], grew
        (t / "hr/late.md").write_text("Zorbina " * 1000)                        # the catalog never reads a file
        read = transport.read
        transport.read = lambda node, path, timeout=60: 1 / 0 if "late" in path else read(node, path, timeout)
        try:
            assert "1 files added" in cloud.read(".")["text"]
        finally:
            transport.read = read
        os.remove(t / "hr/late.md")
        put, tree, sent = transport.put, transport.put_tree, []
        transport.put = lambda node, d, name, data, timeout=60: sent.append(name) or put(node, d, name, data, timeout)
        transport.put_tree = lambda node, d, src, timeout=60: sent.append(src) or tree(node, d, src, timeout)
        try:
            for alias in [a for a in cloud.alias if "review" in a]:             # raw, a file the caller sees
                cloud.offload("x", [alias], {"bool": True}, node="pop")         # scrubbed: its whole scope, once
            for j in cloud.jobs:
                j.join()
        finally:
            transport.put, transport.put_tree = put, tree
        assert sent == [str(t / "hr")], sent
        ref = cloud.offload("x", [str(t / "keys/prod.env")], node="pop")["ref"]   # a hidden job's output stays
        for j in cloud.jobs:
            j.join()
        assert "stays on its machine" in refusal(cloud, "offload", task="x", inputs=[ref], node="two")
        GOT.clear()
        out = pop.offload("Ask Zorbina Quellwater.", [], node="mid")["ref"]     # a raw task's output keeps its
        assert "Zorbina" in GOT[-1][1] and "hr" in pop.refs[out], pop.refs      # scopes: the cloud never gets it
        assert pop.give(pop.item(out), pop.nodes["cloud"]) is None
        sh, sent = transport._sh, []                                            # a hidden job's request: one size
        transport._sh = lambda node, cmd, data=None, timeout=60: sent.append(len(data or b"")) or b"{}"   # per file
        try:
            for name in ("Al_Wu-review.md", "Zorbina_Quellwater-review.md"):
                real_run({"model": {"model": "m", "base_url": "http://x/v1"}}, [f"/s/{name}"], "x", None, "/o/x.txt",
                         keep=True, timeout=5)
        finally:
            transport._sh = sh
        assert sent[1] == sent[3], sent
        assert not any(json.loads(line).get("long") is None and len(line) != 4095 for line in log.read_text().splitlines())


def test_thirty_second_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "hr/more.csv").write_text("name\nMei Park\nTom Hill\nAnna Brown\n")
        (t / "box/hr").mkdir(parents=True)
        (t / "box/hr/people.csv").write_text(CSV)
        node = lambda trust, n, u=None: {"trust": trust, "out": str(t / n / "out"), "stage": str(t / n / "stage"),  # noqa: E731
                                         **({"model": {"url": u, "model": "m", "base_url": u}} if u else {})}
        q = {**p, "scopes": {f"box:{t}/box/hr": "hr", **p["scopes"]},
             "nodes": {**p["nodes"], "box": node(["hr", "hr~", "keys", "keys~"], "box"),
                       "mid": node(["hr~"], "mid", "http://mid/v1")}}
        mid = door.Door({**q, "workspace": str(t / "ws3")}, t / "state3", "mid")
        (t / "ws3").mkdir()
        mid.prepare(lambda scope: lambda text: [])
        assert "another machine" in refusal(mid, "offload", task="x", inputs=[f"box:{t}/box/hr/people.csv"],
                                            returns={"bool": True}, node="pop")  # a scope on a third machine: never
        try:                                                                     # packed from this disk
            door.Door({**q, "nodes": {**q["nodes"], "box": node(["hr", "hr~"], "box", "http://mid/v1")}}, t / "s4", "mid")
            assert False, "two names on one model server"
        except core.Refused as e:
            assert "one model server" in str(e)
        pop = door.Door(p, t / "state", "pop")
        pop.prepare(lambda scope: lambda text: [])
        view = pop.vault.scrub("park88, Park88, brown2024, Hill42, jwhill, jwpark, parkjw. The kingdom, parking.", {"hr"})
        assert not re.search(r"(?i)park\d|brown|hill|jw", view) and "kingdom, parking" in view, view
        assert "park" not in pop.vault.scrub_name("park88-review.md", {"hr"}).lower()


def test_thirty_third_hunt_stays_fixed():
    with tempfile.TemporaryDirectory() as tmp:
        t, p = setup(os.path.realpath(tmp))
        (t / "box/hr").mkdir(parents=True)
        (t / "box/hr/people.csv").write_text("name,email\nAnna Miller,amiller@corp.example\nSam Carter,sc@corp.example\n"
                                             "Rita Edwards,re@corp.example\nAnne Vance,av@corp.example\nZorbina Quellwater,zq@corp.example\n")
        (t / "box/hr/Zorbina_Quellwater-review.md").write_text("Rota: amiller, scarter, redwards. We planned it.\n")
        sh, calls = transport._sh, []
        def remote(node, cmd, data=None, timeout=60):                           # "ssh": run here, and record
            calls.append((cmd, data))                                           # what this machine would see
            return sh({}, cmd, data, timeout)
        transport._sh = remote
        try:
            box = {"ssh": "box", "trust": ["hr", "hr~"], "out": str(t / "bo"), "stage": str(t / "bs")}
            q = {**p, "scopes": {f"box:{t}/box/hr": "hr", **{d: s for d, s in p["scopes"].items() if s != "hr"}},
                 "nodes": {**p["nodes"], "box": box}}
            pop = door.Door({**q, "workspace": str(t / "ws4")}, t / "state4", "pop")
            (t / "ws4").mkdir()
            pop.prepare(lambda scope: lambda text: [])
            assert calls and not any("Zorbina" in c for c, _ in calls), calls  # no path on a command line, and
            assert {len(d) for c, d in calls if "NODE" in c or d and len(d) == 4096} <= {4096}   # one size on the wire
            cloud = door.Door({**q, "workspace": str(t / "ws2")}, t / "state4", "cloud")
            (t / "ws2").mkdir()
            cloud.prepare(lambda scope: lambda text: [])
            calls.clear()
            listing = cloud.read(".")["text"]
            alias = next(a for a in cloud.alias if "review" in a)
            view = cloud.read(alias)["text"]
            assert not calls, calls                                             # nothing crosses at request time
            assert "Zorbina" not in listing and not re.search(r"amiller|scarter|redwards", view), view
            assert "We planned it." in view, view
        finally:
            transport._sh = sh


def test_thirty_fourth_hunt_stays_fixed():
    notes = " ".join("".join(chr(98 + (i * 7 + j * 3) % 24) for j in range(9)) for i in range(6000))
    def world(names):                                                          # a read's time: the same whatever
        with tempfile.TemporaryDirectory() as tmp:                             # the hidden names are
            t, p = setup(os.path.realpath(tmp))
            (t / "hr/people.csv").write_text("name,salary\n" + "".join(f"{n},100\n" for n in names))
            (t / "hr/notes.txt").write_text(notes)
            pop = door.Door(p, t / "state", "pop")
            pop.prepare(lambda scope: lambda text: [])
            cloud = door.Door({**p, "workspace": str(t / "ws2")}, t / "state", "cloud")
            (t / "ws2").mkdir()
            cloud.prepare(lambda scope: lambda text: [])
            cloud.read(".")
            alias = next(a for a in cloud.alias if "notes" in a)
            live, cloud.vault.scrub = cloud.vault.scrub, lambda *a, **k: 1 / 0  # a request scrubs nothing: it looks
            try:                                                                 # the view up
                took = []
                for _ in range(9):
                    t0 = time.perf_counter()
                    assert cloud.read(alias)["row"] == "scrubbed"
                    took.append(time.perf_counter() - t0)
                assert "100" in cloud.read(next(a for a in cloud.alias if "people" in a))["text"]
            finally:
                cloud.vault.scrub = live
            return sorted(took)[4]
    a, b = world(["Will Brown", "Rose Green"]), world(["Will Bronx", "Rose Green"])
    assert abs(a - b) < max(a, b) * 0.5 + 0.002, (a, b)                       # it was 22 ms against 217


def test_own_precision():
    """Caller text: everyday and technical words stay as written; names, in any of the forms the hunts used, do not."""
    import scrub
    v = scrub.Vault(tempfile.mkdtemp() + "/v.json")
    text = ("What is the average salary in the CSV? Offload the env file from the workspace and check the stdout. "
            "Mail Zorbina about quellwater, and tell Will, May and frank that Strand left. Grace Friday, January Jones "
            "and Mary I. Baker agree; ask knapper and barrio and judge and lord and shadow, and sk and pk, aino repo and hanna auth.")
    out = v.own(scrub.norm(text), {}, {"hr"})
    words = set(re.findall(r"[\w']+", out))
    for keep in ("the", "average", "salary", "file", "workspace"):
        assert keep in words, (keep, out)
    for name in ("Zorbina", "quellwater", "Will", "May", "frank", "Strand", "Grace", "Friday", "January", "Jones",
                 "Mary", "Baker", "knapper", "barrio", "judge", "lord", "shadow", "sk", "pk", "aino", "repo", "auth"):
        assert name not in words, (name, out)


def tmp_out():
    return tempfile.mkdtemp() + "/out.txt"


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok  ", name)
