# claude / notice

**result:** PASS · 37.1 s · offloads 1 · refused 0 · bits {'hr': 0, 'acme': 9, 'legal': 0} · leaked none

**prompt to the agent:** Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-75bbf2c1/acme/contract.md requires for termination. End your reply with just the number.

## Cloud side: the agent in the room (everything that crossed the route)

3 model calls through the route; main thread is call 3 (model `claude-opus-5-5`, tools: Agent, Bash, Edit, ListAgents, Read, ReportFindings, ScheduleWakeup, Skill, ToolSearch, Workflow, Write, DeferredToolPlaceholder, mcp__lego__offload).


#### user
[system-reminder, 565 chars]


Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-75bbf2c1/acme/contract.md requires for termination. End your reply with just the number.

#### system
[harness context, 4020 chars, not shown]

#### assistant
**→ ToolSearch**
```json
{
 "query": "select:mcp__lego__offload",
 "max_results": 1
}
```

#### user
**← result**
```
[tool_reference]
```

Tool loaded.

#### assistant
**→ mcp__lego__offload**
```json
{
 "task": "How many days of notice does this contract require for termination? Answer with the number of days.",
 "inputs": [
  "pop:/tmp/lego-e2e-75bbf2c1/acme/contract.md"
 ],
 "returns": {
  "int": [
   0,
   365
  ]
 }
}
```

#### user
**← result**
```
{"row": "shaped", "answer": 95, "ref": "pop:/tmp/lego-e2e-75bbf2c1/out/7b7e3a36f960e326e4cabdef8d83d50b.txt", "allowance_left": {"hr": 16, "acme": 7, "legal": 16}}
```

#### assistant
> *thinking:* (redacted)

The offload tool reports that the contract requires 95 days' notice for termination. Because the contract is a private file, the tool only returns the number itself, so I can't quote the clause. The model's full answer was saved for you at `pop:/tmp/lego-e2e-75bbf2c1/out/7b7e3a36f960e326e4cabdef8d83d50b.txt`.

95

## Private side: the local model on gpu-box (never left the node)

#### job on `pop`: row **shaped**, scopes ['acme'], 9 bits charged

**prompt** (`deepseek-v4.1-flash-1017`, constrained: True)
```
How many days of notice does this contract require for termination? Answer with the number of days.

--- /tmp/lego-e2e-75bbf2c1/acme/contract.md ---
# Master services agreement ACME-E81449
Acme Corp and the Company.

## 9. Termination
Either party may terminate on ninety-five (95) days written notice.

```

**reasoning**
```
We need answer question based only file. Contract says ninety-five (95) days. Asked number of days. Answer 95. Need be concise.
```

**output** (stays on the node; the cloud gets only the reply row)
```
{"answer": 95}
```

usage: {'prompt_tokens': 126, 'total_tokens': 164, 'completion_tokens': 38, 'prompt_tokens_details': {'cached_tokens': 0, 'created_cache_tokens': 0, 'multimodal_tokens': None}, 'completion_tokens_details': {'reasoning_tokens': 30}}
