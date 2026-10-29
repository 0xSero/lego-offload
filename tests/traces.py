"""Render one e2e case as a readable trace: what the cloud model saw and said (from the route's
capture), and what the local model saw and said (its prompt with the private files, its
reasoning, its output), next to what the door let back out. Email addresses are redacted;
the cloud system prompt and tool schemas are summarized, not copied."""
import json
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def clean(s, n=4000):
    s = REMINDER.sub(lambda m: f"[system-reminder, {len(m.group())} chars]", EMAIL.sub("<email>", str(s)))
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more chars]"


def sse(raw):
    """Rebuild the assistant message from a streamed reply."""
    blocks = {}
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except ValueError:
            continue
        i = ev.get("index")
        if ev.get("type") == "content_block_start":
            blocks[i] = dict(ev["content_block"], text=ev["content_block"].get("text", ""), buf="")
        elif ev.get("type") == "content_block_delta":
            d = ev["delta"]
            blocks[i]["buf"] += d.get("text") or d.get("thinking") or d.get("partial_json") or ""
    out = []
    for b in (blocks[i] for i in sorted(blocks)):
        if b["type"] == "tool_use":
            out.append({"type": "tool_use", "name": b["name"], "input": json.loads(b["buf"] or "{}")})
        elif b["type"] in ("text", "thinking"):
            out.append({"type": b["type"], b["type"]: b["text"] + b["buf"]})
    return out


def show(block):
    t = block.get("type")
    if t == "text":
        return clean(block["text"])
    if t == "thinking":
        return "> *thinking:* " + clean(block.get("thinking") or "(redacted)").replace("\n", "\n> ")
    if t == "tool_use":
        return f"**→ {block['name']}**\n```json\n{json.dumps(block['input'], indent=1)}\n```"
    if t == "tool_result":
        c = block.get("content")
        c = "\n".join(x.get("text", f"[{x.get('type')}]") for x in c) if isinstance(c, list) else c
        return f"**← result**\n```\n{clean(c, 1500)}\n```"
    return f"[{t}]"


def cloud(capture):
    recs = [json.loads(l) for l in open(capture)] if capture else []
    convo = [r for r in recs if r["path"].startswith("/v1/messages") and r["request"].startswith("{")]
    if not convo:
        return ["*(no cloud model in this case)*"]
    reqs = [json.loads(r["request"]) for r in convo]
    main = max(range(len(reqs)), key=lambda i: (len(reqs[i].get("messages", [])), i))
    req = reqs[main]
    sys_len = sum(len(b.get("text", "")) for b in req.get("system", [])) if isinstance(req.get("system"), list) \
        else len(req.get("system") or "")
    out = [f"{len(convo)} model calls through the route; main thread is call {main + 1} "
           f"(model `{req.get('model')}`, system prompt {sys_len} chars, tools: "
           f"{', '.join(t.get('name', '?') for t in req.get('tools', []))}).\n"]
    for m in req["messages"] + [{"role": "assistant", "content": sse(convo[main]["response"])}]:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if m["role"] not in ("user", "assistant"):              # harness context (environment, tool lists)
            out.append(f"#### {m['role']}\n[harness context, {len(json.dumps(content))} chars, not shown]")
            continue
        out.append(f"#### {m['role']}\n" + "\n\n".join(show(b) for b in content))
    return out


def local(jobs):
    if not jobs:
        return ["*(no job reached a local model)*"]
    out = []
    for ev, tr in jobs:
        out.append(f"#### job on `{ev['node']}`: row **{ev['row']}**, scopes {ev['scopes'] or 'public'}, "
                   f"{ev['bits']} bits charged")
        if not tr:
            out.append("*(no trace: the job failed or was not started)*")
            continue
        msgs = tr["request"]["messages"]
        msg = tr["response"]["choices"][0]["message"]
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        out += [f"**prompt** (`{tr['request']['model']}`, constrained: "
                f"{'response_format' in tr['request']})\n```\n" + clean(msgs[-1]["content"], 3000) + "\n```",
                "**reasoning**\n```\n" + (clean(reasoning, 3000) or "(none)") + "\n```",
                "**output** (stays on the node; the cloud gets only the reply row)\n```\n" + clean(msg["content"], 2000)
                + "\n```", f"usage: {tr['response'].get('usage')}"]
    return out


def render(name, prompt, capture, jobs, row):
    return "\n\n".join([f"# {name}", f"**result:** {'PASS' if row['pass'] else 'FAIL'} · {row['seconds']} s · "
                        f"offloads {row['offloads']} · refused {row['refused']} · bits {row['bits_charged']} · "
                        f"leaked {row['leaked'] or 'none'}",
                        "**prompt to the agent:** " + clean(prompt),
                        "## Cloud side: Claude Code in the room (everything that crossed the route)"] + cloud(capture)
                       + ["## Private side: the local model on gpu-box (never left the node)"] + local(jobs)) + "\n"
