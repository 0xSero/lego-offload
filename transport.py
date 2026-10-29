"""Transport: carries out what core.move and core.run decided, on real machines.

A node is local (runs here) or reached over ssh. Bytes always pass through this host,
which policy requires to be trusted with every scope. Jobs run on the node holding the
inputs and call that node's own model, so private text never crosses the network to it.
"""
import json
import secrets
import shlex
import subprocess

RUNNER = r'''
import json, sys, time, urllib.request
job = json.loads(sys.stdin.read())
docs = "".join("\n--- %s ---\n%s" % (p, open(p, errors="replace").read()) for p in job["files"])
body = {"model": job["model"]["model"], "messages": [
    {"role": "system", "content": "You work for the owner of these files. Answer the task from the files only."},
    {"role": "user", "content": job["task"] + "\n" + docs}]}
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


def read(node, path):
    return _sh(node, "cat " + shlex.quote(path))


def copy(src_node, src_path, dst_node, dst_dir):
    """Copy one file between nodes (through this host) and return its path on dst."""
    dst = f"{dst_dir}/{secrets.token_hex(8)}-{src_path.rsplit('/', 1)[-1]}"
    _sh(dst_node, f"mkdir -p {shlex.quote(dst_dir)} && cat > {shlex.quote(dst)}", data=read(src_node, src_path))
    return dst


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
    written to out_path on the node; it comes back only when keep is False."""
    job = {"files": files, "task": task, "schema": schema_json(schema) if schema else None,
           "model": node["model"], "out": out_path, "keep": keep, "timeout": timeout, "salt": salt}
    _sh(node, f"mkdir -p {shlex.quote(out_path.rsplit('/', 1)[0])}")
    raw = _sh(node, "python3 -c " + shlex.quote(RUNNER), data=json.dumps(job).encode(), timeout=timeout + 30)
    return json.loads(raw)


def answer(output):
    """Pull the typed answer out of a constrained JSON reply (anything odd -> None)."""
    try:
        return json.loads(output)["answer"]
    except (ValueError, TypeError, KeyError):
        return None
