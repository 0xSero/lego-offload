"""Transport: carries out what core.move and core.run decided, on real machines.

A node is local (runs here) or reached over ssh. Bytes always pass through this host,
which policy requires to be trusted with every scope. Jobs run on the node holding the
inputs and call that node's own model, so private text never crosses the network to it.
"""
import json
import os
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time

OPEN = r'''
import os, stat, sys
def opn(p):                                    # one component at a time from /, no link followed on the way
    fd = os.open("/", os.O_RDONLY)
    for x in [x for x in p.split("/") if x]:
        try:
            n = os.open(x, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        fd = n
    return fd
def tree(top):                                 # (path, fd) of everything at or under top but folders; a link is
    fd = opn(top)                              # never entered, each folder opened from its parent's descriptor
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        yield top, fd
        return
    stack = [(top, fd, sorted(os.listdir(fd), reverse=True))]
    while stack:
        p, d, names = stack[-1]
        if not names:
            os.close(d)
            stack.pop()
            continue
        e = names.pop()
        try:
            n = os.open(e, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except OSError:
            continue
        if not stat.S_ISDIR(os.fstat(n).st_mode):
            yield p + "/" + e, n
            continue
        try:
            stack.append((p + "/" + e, n, sorted(os.listdir(n), reverse=True)))
        except OSError:
            os.close(n)
def regular(top):                              # (path, bytes) of each regular file at or under top
    try:
        for f, fd in tree(top):
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    yield f, b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
            finally:
                os.close(fd)
    except OSError:
        return
'''

NODE = OPEN + r'''
mode, p = sys.argv[1], sys.argv[2]
if mode == "files":
    sys.stdout.write("".join(f + "\0" for f, _ in regular(p)))
elif mode == "cat":
    fd = opn(p)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        sys.exit("not a regular file")
    for b in iter(lambda: os.read(fd, 1 << 20), b""):
        sys.stdout.buffer.write(b)
else:
    try:
        m = os.fstat(opn(p)).st_mode
    except FileNotFoundError:
        m = None
    if m is not None and not (stat.S_ISDIR(m) or stat.S_ISREG(m)):
        sys.exit("not a regular file")
    print("none" if m is None else "dir" if stat.S_ISDIR(m) else "reg")
'''

RUNNER = OPEN + r'''
import json, signal, time, urllib.request
t0 = time.time()
job = json.loads(sys.stdin.read())
hold = job["keep"]                             # a hidden job: one reply of one size at one time, whatever happened
def stop(*_):
    raise TimeoutError
signal.signal(signal.SIGALRM, stop)
signal.setitimer(signal.ITIMER_REAL, job["timeout"] - (0.75 if hold else 0))   # the whole job, not each read
res = {"took": 0.0, "output": None, **({"answer": None} if job.get("shaped") else {})}
try:
    docs = "".join("\n--- %s ---\n%s" % (f, b.decode(errors="replace")) for p in job["files"] for f, b in regular(p))
    body = {"model": job["model"]["model"], "messages": [
        {"role": "system", "content": "You work for the owner of these files. Answer the task from the files given, or "
         "from what you know if none are. Placeholders like [PERSON_3f9a] stand for real values: use them as the values, "
         "copied exactly."},
        {"role": "user", "content": job["task"] + "\n" + docs}]}
    if "temperature" in job:
        body["temperature"] = job["temperature"]
    if job.get("salt"):
        body["cache_salt"] = job["salt"]       # prompt cache shared only by jobs with the same scopes
    if job["schema"]:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": job["schema"]}}
    req = urllib.request.Request(job["model"]["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=job["timeout"]).read())
    out = resp["choices"][0]["message"]["content"] or ""
    open(job["out"], "w").write(out)
    json.dump({"request": body, "response": resp}, open(job["out"] + ".trace.json", "w"))  # stays on the node
    res = {"took": time.time() - t0, "output": None if hold else out}
    if job.get("shaped"):                      # only the answer comes back, parsed here
        try:
            res["answer"] = json.loads(out.strip().removeprefix("```json").removesuffix("```"))["answer"]
        except Exception:
            res["answer"] = None
        if len(json.dumps(res["answer"])) > 4096:
            res["answer"] = None
except BaseException:
    if not hold:
        raise
    res = {"took": 0.0, "output": None, **({"answer": None} if job.get("shaped") else {})}
signal.setitimer(signal.ITIMER_REAL, 0)
if hold:                                       # released at the end of its slot, never when the model is done
    time.sleep(max(0.0, t0 + job["timeout"] - 0.5 - time.time()))
print(json.dumps(res).ljust(8192) if hold else json.dumps(res))
'''


def _sh(node, cmd, data=None, timeout=60):
    """Run a shell command on a node; returns stdout bytes."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node["ssh"], cmd] if node.get("ssh") \
        else ["/bin/sh", "-c", cmd]
    r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{cmd.split()[0]} failed on {node.get('ssh', 'local')}: {r.stderr.decode()[-300:]}")
    return r.stdout


def _node(node, mode, path, timeout):
    return _sh(node, "python3 -c " + shlex.quote(NODE) + f" {mode} " + shlex.quote(path), timeout=timeout)


def kind(node, path, timeout=60):
    """"reg", "dir", or None when there is nothing there. Raises RuntimeError on a link anywhere on
    the way, or on any other kind of file: what a node holds is looked at without following links."""
    if node.get("ssh"):
        out = _node(node, "kind", path, timeout).decode().strip()
        return None if out == "none" else out
    try:
        fd = nofollow(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeError("a link on the way") from None
    try:
        m = os.fstat(fd).st_mode
    finally:
        os.close(fd)
    if not (stat.S_ISDIR(m) or stat.S_ISREG(m)):
        raise RuntimeError("not a regular file")
    return "dir" if stat.S_ISDIR(m) else "reg"


def read(node, path, timeout=60):
    """A regular file's bytes, with no link followed anywhere on its path, on any node."""
    if node.get("ssh"):
        return _node(node, "cat", path, timeout)
    try:
        fd = nofollow(path)                                     # here: no process, never waits on a FIFO
    except OSError as e:
        raise RuntimeError(f"cat failed on local: {e.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("cat failed on local: not a regular file")
        return b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
    finally:
        os.close(fd)


def files(node, root):
    """The regular files under a directory on a node, sorted."""
    if not node.get("ssh"):                                     # here: what `find -type f` lists, no process
        return sorted(_regular(root))
    out = _node(node, "files", root, 120)                       # there, as here: no link entered; names may hold newlines
    return sorted(f for f in out.decode(errors="replace").split("\0") if f)


def nofollow(path, flags=os.O_RDONLY):
    """A descriptor for `path`, opened one component at a time from / without following any link, so
    a link anywhere on the way (one swapped in a moment ago, too) raises OSError instead of being followed."""
    sep = b"/" if isinstance(path, bytes) else "/"
    parts = [p for p in path.split(sep) if p]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, p in enumerate(parts):
            nfd = os.open(p, (flags if i == len(parts) - 1 else os.O_RDONLY | os.O_DIRECTORY) | os.O_NOFOLLOW |
                          os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular(top):
    """Regular files at or under `top`: links neither followed nor listed (not even on the way to top, or
    swapped in during the walk: each folder is opened from its parent's descriptor), unreadable folders skipped."""
    return walk(top, lambda e: e.is_file(follow_symlinks=False))


def walk(top, keep, skip=lambda name: False):
    """Entries under `top` that are not folders and that keep(entry) accepts (a link is such an entry, never
    entered), never entering a folder whose name skip() accepts; each folder opened from its parent's descriptor."""
    try:
        fd = nofollow(top)
    except OSError:
        return []
    mode = os.fstat(fd).st_mode
    if not stat.S_ISDIR(mode):
        os.close(fd)
        return [top] if stat.S_ISREG(mode) else []
    out, stack = [], []

    def enter(p, d):
        subs = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subs += [] if skip(e.name) else [e.name]
                        elif keep(e):
                            out.append(os.path.join(p, e.name))
                    except OSError:
                        pass
        except OSError:
            pass
        stack.append((p, d, subs))
    enter(top, fd)
    while stack:                                               # a loop, not recursion: any depth
        p, d, subs = stack[-1]
        if not subs:
            os.close(d)
            stack.pop()
            continue
        n = subs.pop()
        try:
            enter(os.path.join(p, n), os.open(n, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=d))
        except OSError:
            pass
    return out


def put(node, dst_dir, name, data, timeout=60):
    """Write bytes into a new file on a node and return its path."""
    dst = f"{dst_dir}/{secrets.token_hex(8)}-{name}"
    if node.get("ssh"):
        _sh(node, f"mkdir -p {shlex.quote(dst_dir)} && cat > {shlex.quote(dst)}", data=data, timeout=timeout)
        return dst
    os.makedirs(dst_dir, exist_ok=True)                          # here: no process
    with open(dst, "xb") as f:
        f.write(data)
    return dst


def put_tree(node, dst_dir, src, timeout=60):
    """Copy a file or directory from this host into a new folder on a node, in one pipe: the door does
    the same work however many files it holds. Returns its path on the node."""
    dst = f"{dst_dir}/{secrets.token_hex(8)}"
    parent, base = os.path.split(src.rstrip("/"))
    unpack = f"mkdir -p {shlex.quote(dst)} && tar -C {shlex.quote(dst)} -xf -"
    sink = f"ssh -o BatchMode=yes -o ConnectTimeout=10 {shlex.quote(node['ssh'])} {shlex.quote(unpack)}" if node.get("ssh") \
        else "/bin/sh -c " + shlex.quote(unpack)
    p = subprocess.Popen(["/bin/sh", "-c", f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(base)} | {sink}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)                       # both tars and ssh, not just the shell
        p.wait()
        raise
    if p.returncode:
        raise RuntimeError("staging failed")
    return f"{dst}/{base}"


def schema_json(s):
    """core's answer grammar -> JSON schema for constrained decoding."""
    if s.get("bool"):
        inner = {"type": "boolean"}
    elif "enum" in s:
        inner = {"enum": s["enum"]}
    elif "int" in s:
        inner = {"type": "integer", "minimum": s["int"][0], "maximum": s["int"][1]}
    else:
        items = [schema_json(x)["properties"]["answer"] for x in s["tuple"]]
        inner = {"type": "array", "prefixItems": items, "minItems": len(items), "maxItems": len(items)}
    return {"type": "object", "properties": {"answer": inner}, "required": ["answer"], "additionalProperties": False}


def run_job(node, files, task, schema, out_path, keep, timeout, salt=None, shaped=False):
    """Run one model call on `node` over `files` (paths on that node). The full output is
    written to out_path on the node; it comes back only when keep is False. `shaped`: only the parsed
    answer comes back, in a reply of fixed size. The job is over a second before `timeout`, killed if
    need be, so the node is idle when the bound the door booked comes."""
    timeout = max(0.1, timeout - 1.0)
    if node["model"].get("api") == "anthropic":                # in a child killed at the bound: the door itself
        job = {"node": node, "files": files, "task": task, "schema": schema, "out_path": out_path, "keep": keep,
               "timeout": timeout}                             # does no work that grows with the files
        r = subprocess.run([sys.executable, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import transport; "
                            "print(json.dumps(transport.run_anthropic(**json.load(sys.stdin))))",
                            os.path.dirname(os.path.abspath(__file__))], input=json.dumps(job).encode(),
                           capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError("cloud job failed")
        return json.loads(r.stdout)
    job = {"files": files, "task": task, "schema": schema_json(schema) if schema else None, "shaped": shaped,
           "model": node["model"], "out": out_path, "keep": keep or shaped, "timeout": timeout, "salt": salt}
    t0 = time.time()
    _sh(node, f"mkdir -p {shlex.quote(out_path.rsplit('/', 1)[0])}", timeout=timeout)
    job["timeout"] = max(0.05, timeout - (time.time() - t0))
    raw = _sh(node, "python3 -c " + shlex.quote(RUNNER) + (" 2>/dev/null" if job["keep"] else ""),   # no traceback
              data=json.dumps(job).encode(), timeout=job["timeout"] + 0.5)   # crosses; it ends half a second early
    return json.loads(raw)


IDS = {"type": "object", "additionalProperties": False, "required": ["identifiers"], "properties": {"identifiers": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["kind", "text"], "properties": {
        "kind": {"enum": ["PERSON", "ADDRESS", "USERNAME", "ID"]}, "text": {"type": "string"}}}}}}


def find_ids(node, text):
    """Ask a node's model (trusted with the text) for the identifiers in it: [(kind, text)], window by window."""
    return list(dict.fromkeys(x for i in range(0, max(len(text), 1), 50000) for x in _find(node, text[i:i + 52000])))


def _find(node, text):
    task = ("List every personal identifier in the text below: full names and lone first or last names of people, "
            "street addresses, usernames and handles, personal ID or account numbers. Copy each exactly as written. "
            "Not company names, job titles, places alone, dates or amounts.\n\n" + text)
    out = f"{node['stage']}/ids-{secrets.token_hex(8)}.txt"
    job = {"files": [], "task": task, "schema": IDS, "model": node["model"], "out": out, "keep": False, "timeout": 600,
           "temperature": 0}
    _sh(node, f"mkdir -p {shlex.quote(node['stage'])}")
    r = json.loads(_sh(node, "python3 -c " + shlex.quote(RUNNER), data=json.dumps(job).encode(), timeout=630))
    try:
        return [(i["kind"], i["text"]) for i in json.loads(r["output"])["identifiers"]]
    except (ValueError, KeyError, TypeError):
        return []


def answer(output):
    """Pull the typed answer out of a constrained JSON reply (anything odd -> None)."""
    try:
        return json.loads(output.strip().removeprefix("```json").removesuffix("```"))["answer"]
    except Exception:                                           # RecursionError too: any odd reply
        return None


def run_anthropic(node, files, task, schema, out_path, keep, timeout):
    """A cloud model as a node: the files were written to this host's stage, the call goes out from here
    with the user's Claude login. No output cap: max_tokens is the model's own maximum."""
    import urllib.request
    import route
    docs = "".join(f"\n--- {f.rsplit('/', 1)[-1].split('-', 1)[-1]} ---\n" + read({}, f).decode(errors="replace")
                   for p in files for f in sorted(_regular(p)))
    sys = ("Answer the task from the files given, or from what you know if none are. Placeholders like [PERSON_3f9a] "
           "stand for real values: use them as the values, copied exactly.")
    if schema:
        sys += " Reply with only a JSON object matching: " + json.dumps(schema_json(schema))
    sys = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},  # the login's
           {"type": "text", "text": sys}]                                                          # required opener
    body = {"model": node["model"]["model"], "max_tokens": node["model"].get("max_tokens", 128000), "stream": True,
            "system": sys, "messages": [{"role": "user", "content": task + "\n" + docs}]}
    t0 = time.time()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
        **route.claude_auth()})
    out = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if time.time() > t0 + timeout:                      # the whole call, not each read, is bounded
                raise TimeoutError("cloud job ran past its limit")
            if line.startswith(b"data:"):
                ev = json.loads(line[5:])
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    out.append(ev["delta"]["text"])
    text = "".join(out)
    os.makedirs(out_path.rsplit("/", 1)[0], exist_ok=True)
    open(out_path, "w").write(text)
    json.dump({"request": body, "response": text}, open(out_path + ".trace.json", "w"))   # what the cloud got
    return {"took": time.time() - t0, "output": None if keep else text}
