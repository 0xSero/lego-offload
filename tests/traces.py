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


def turns_anthropic(req, resp):
    out = []
    for m in req["messages"] + [{"role": "assistant", "content": sse(resp)}]:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        out.append((m["role"], [show(b) for b in content]))
    return out


def turns_responses(req, resp):
    """OpenAI Responses API (Codex): input items, then the streamed output items."""
    done = [json.loads(l[6:])["item"] for l in resp.splitlines()
            if l.startswith("data: ") and '"response.output_item.done"' in l]
    out = []
    for it in req.get("input", []) + done:
        t = it.get("type")
        if t == "message":
            out.append((it["role"], [clean(c.get("text", "")) for c in it.get("content", [])]))
        elif t == "function_call":
            out.append(("assistant", [show({"type": "tool_use", "name": it["name"],
                                            "input": json.loads(it.get("arguments") or "{}")})]))
        elif t == "function_call_output":
            out.append(("user", [show({"type": "tool_result", "content": str(it.get("output"))})]))
        elif t == "reasoning":
            out.append(("assistant", ["> *reasoning:* " + clean(" ".join(x.get("text", "") for x in it.get("summary", [])))
                                      or "> *reasoning (encrypted)*"]))
    return out


def turns_chat(req, resp):
    """OpenAI chat completions (Cline): messages, then the streamed reply."""
    text, calls = "", {}
    for l in resp.splitlines():
        if l.startswith("data: {"):
            for ch in json.loads(l[6:]).get("choices", []):
                d = ch.get("delta", {})
                text += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc["index"], {"name": "", "args": ""})
                    c["name"] += tc.get("function", {}).get("name") or ""
                    c["args"] += tc.get("function", {}).get("arguments") or ""
    out = []
    for m in req.get("messages", []):
        c = m.get("content")
        c = "\n".join(x.get("text", "") for x in c) if isinstance(c, list) else c or ""
        body = [show({"type": "tool_result", "content": c})] if m["role"] == "tool" else [clean(c)] if c else []
        body += [f"**→ {t['function']['name']}**\n```json\n{clean(t['function']['arguments'], 1500)}\n```"
                 for t in m.get("tool_calls") or []]
        out.append((m["role"], body))
    out.append(("assistant", ([clean(text)] if text else []) +
                [f"**→ {c['name']}**\n```json\n{clean(c['args'], 1500)}\n```" for c in calls.values()]))
    return out


def cloud(capture):
    recs = [json.loads(l) for l in open(capture)] if capture else []
    if any(r["path"].startswith(("/aiserver.", "/agent.")) for r in recs):
        return cursor_view(recs)
    convo = [r for r in recs if r["request"].startswith("{") and
             any(k in r["path"] for k in ("/v1/messages", "/responses", "/chat/completions"))]
    if not convo:
        return ["*(no cloud model in this case)*"]
    reqs = [json.JSONDecoder().raw_decode(r["request"])[0] for r in convo]     # hex the route decoded follows
    size = lambda q: len(q.get("messages") or q.get("input") or [])
    main = max(range(len(reqs)), key=lambda i: (size(reqs[i]), i))
    req, resp, path = reqs[main], convo[main]["response"], convo[main]["path"]
    tools = req.get("tools") or [t for it in req.get("input", []) if it.get("type") == "additional_tools"
                                 for t in it.get("tools", [])]
    kind = turns_anthropic if "/v1/messages" in path else turns_responses if "/responses" in path else turns_chat
    out = [f"{len(convo)} model calls through the route; main thread is call {main + 1} "
           f"(model `{req.get('model')}`, tools: "
           f"{', '.join(t.get('name') or t.get('function', {}).get('name', '?') for t in tools)}).\n"]
    for role, body in kind(req, resp):
        if role not in ("user", "assistant", "tool"):          # harness context (system prompt, environment)
            out.append(f"#### {role}\n[harness context, {sum(len(b) for b in body)} chars, not shown]")
        else:
            out.append(f"#### {role}\n" + "\n\n".join(body))
    return out


def cursor_view(recs):
    """Cursor speaks protobuf; show the readable strings of the agent conversation, in order."""
    seen, lines = set(), []
    for r in recs:
        if "BidiAppend" not in r["path"] and "RunSSE" not in r["path"]:
            continue
        for side in ("request", "response"):
            for m in re.findall(r"[\x20-\x7e\n\t\u00a0-\uffff]{12,}", r[side]):
                m = m.strip()
                if " " in m and m not in seen and "\ufffd" not in m:
                    seen.add(m)
                    lines.append(("→ cloud" if side == "request" else "← cloud", m))
    return [f"{len(recs)} exchanges over Cursor's protobuf API; readable strings of the agent conversation "
            f"({len(lines)}), in order:\n"] + [f"**{d}**\n```\n{clean(m, 1500)}\n```" for d, m in lines[:120]]


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
                        "## Cloud side: the agent in the room (everything that crossed the route)"] + cloud(capture)
                       + ["## Private side: the local model on gpu-box (never left the node)"] + local(jobs)) + "\n"
