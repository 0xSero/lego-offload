"""Transport: carries out what core.move and core.run decided, on real machines.

A node is local (runs here) or reached over ssh. Bytes always pass through this host,
which policy requires to be trusted with every scope. Jobs run on the node holding the
inputs and call that node's own model, so private text never crosses the network to it.
"""
import json
import os
import secrets
import shlex
import stat
import subprocess
import time

RUNNER = r'''
import json, signal, sys, time, urllib.request
job = json.loads(sys.stdin.read())
signal.setitimer(signal.ITIMER_REAL, job["timeout"])     # the whole job, not each read: SIGALRM ends it
import os
walk = lambda p: [] if os.path.islink(p) else sorted(f for r, _, fs in os.walk(p) for f in [os.path.join(r, x) for x in fs]
                                                     if not os.path.islink(f)) if os.path.isdir(p) else [p]   # links: skipped
docs = "".join("\n--- %s ---\n%s" % (f, open(f, errors="replace").read()) for p in job["files"] for f in walk(p))
body = {"model": job["model"]["model"], "messages": [
    {"role": "system", "content": "You work for the owner of these files. Answer the task from the files given, or "
     "from what you know if none are. Placeholders like [PERSON_3f9a] stand for real values: use them as the values, "
     "copied exactly."},
    {"role": "user", "content": job["task"] + "\n" + docs}]}
if "temperature" in job:
    body["temperature"] = job["temperature"]
if job.get("salt"):
    body["cache_salt"] = job["salt"]     # prompt cache shared only by jobs with the same scopes
if job["schema"]:
    body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": job["schema"]}}
t0 = time.time()
req = urllib.request.Request(job["model"]["base_url"].rstrip("/") + "/chat/completions",
                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
resp = json.loads(urllib.request.urlopen(req, timeout=job["timeout"]).read())
out = resp["choices"][0]["message"]["content"] or ""
open(job["out"], "w").write(out)
json.dump({"request": body, "response": resp}, open(job["out"] + ".trace.json", "w"))  # stays on the node
print(json.dumps({"took": time.time() - t0, "output": None if job["keep"] else out}))
'''


def _sh(node, cmd, data=None, timeout=60):
    """Run a shell command on a node; returns stdout bytes."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node["ssh"], cmd] if node.get("ssh") \
        else ["/bin/sh", "-c", cmd]
    r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{cmd.split()[0]} failed on {node.get('ssh', 'local')}: {r.stderr.decode()[-300:]}")
    return r.stdout


def read(node, path, timeout=60):
    if node.get("ssh"):
        return _sh(node, "cat " + shlex.quote(path), timeout=timeout)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)              # here: no process, and never waits on a FIFO
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("cat failed on local: not a regular file")
        return b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
    finally:
        os.close(fd)


def files(node, root):
    """The regular files under a directory on a node, sorted."""
    if not node.get("ssh"):                                     # here: what `find -type f` lists, no process
        return sorted(f.decode(errors="replace") for f in _regular(os.fsencode(root)))
    out = _sh(node, f"find {shlex.quote(root)} -type f -print0 2>/dev/null; true", timeout=120)   # names may hold newlines
    return sorted(f for f in out.decode(errors="replace").split("\0") if f)


def _regular(top):
    """Regular files at or under `top`: links neither followed nor listed, unreadable folders skipped."""
    try:
        mode = os.lstat(top).st_mode
    except OSError:
        return []
    if not stat.S_ISDIR(mode):
        return [top] if stat.S_ISREG(mode) else []
    out, todo = [], [top]
    while todo:                                                # a loop, not recursion: any depth
        try:
            entries = list(os.scandir(todo.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    todo.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    out.append(e.path)
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
    r = subprocess.run(["/bin/sh", "-c", f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(base)} | {sink}"],
                       capture_output=True, timeout=timeout)
    if r.returncode:
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


def run_job(node, files, task, schema, out_path, keep, timeout, salt=None):
    """Run one model call on `node` over `files` (paths on that node). The full output is
    written to out_path on the node; it comes back only when keep is False. The job is over a second
    before `timeout`, killed if need be, so the node is idle when the bound the door booked comes."""
    timeout = max(0.1, timeout - 1.0)
    if node["model"].get("api") == "anthropic":
        return run_anthropic(node, files, task, schema, out_path, keep, timeout)
    job = {"files": files, "task": task, "schema": schema_json(schema) if schema else None,
           "model": node["model"], "out": out_path, "keep": keep, "timeout": timeout, "salt": salt}
    t0 = time.time()
    _sh(node, f"mkdir -p {shlex.quote(out_path.rsplit('/', 1)[0])}", timeout=timeout)
    job["timeout"] = max(0.05, timeout - (time.time() - t0))
    raw = _sh(node, "python3 -c " + shlex.quote(RUNNER), data=json.dumps(job).encode(), timeout=job["timeout"])
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
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def run_anthropic(node, files, task, schema, out_path, keep, timeout):
    """A cloud model as a node: the files were written to this host's stage, the call goes out from here
    with the user's Claude login. No output cap: max_tokens is the model's own maximum."""
    import urllib.request
    import route
    walk = lambda p: [] if os.path.islink(p) else sorted(  # noqa: E731
        f for r, _, fs in os.walk(p) for f in [os.path.join(r, x) for x in fs] if not os.path.islink(f)) if os.path.isdir(p) else [p]
    docs = "".join(f"\n--- {f.rsplit('/', 1)[-1].split('-', 1)[-1]} ---\n" + open(f, errors="replace").read()
                   for p in files for f in walk(p))
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
