"""The exchange rule, both ways, with a stub model (no network): what each model is given is what
its level allows, scrubbed text holds no identifier, a model that sees everything gets real names
back, and links or workspace copies do not launder a label."""
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LEGO_TAINT"] = tempfile.mkdtemp() + "/taint.json"           # not the user's
import door  # noqa: E402
door.same_disk = lambda a, b: False                          # stub nodes: no real disk to measure
import transport  # noqa: E402

GOT = []                                                       # (node, what the model was given)


def fake_job(node, files, task, returns, out, keep, timeout, salt=None):     # the files are staged on this host
    walk = [x for f in files for x in (sorted(p for p in Path(f).rglob("*") if p.is_file()) if Path(f).is_dir() else [Path(f)])]
    seen = task + "".join(x.name + x.read_text() for x in walk)
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
        try:                                                                  # a local node on the room's disk
            door.same_disk = lambda a, b: True
            door.Door(p, t / "s5", "cloud")
            raise AssertionError("a local node on the workspace disk was accepted")
        except door.core.Refused:
            pass
        finally:
            door.same_disk = lambda a, b: False

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


def tmp_out():
    return tempfile.mkdtemp() + "/out.txt"


if __name__ == "__main__":
    for f in (test_levels, test_forward_cloud_in_front, test_reverse_local_in_front, test_found_leaks_stay_fixed,
              test_second_hunt_stays_fixed, test_third_hunt_stays_fixed, test_fourth_hunt_stays_fixed,
              test_fifth_hunt_stays_fixed, test_sixth_hunt_stays_fixed, test_seventh_hunt_stays_fixed,
              test_eighth_hunt_stays_fixed, test_ninth_hunt_stays_fixed, test_tenth_hunt_stays_fixed):
        f()
        print("ok  ", f.__name__)
