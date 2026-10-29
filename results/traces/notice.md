# notice

**result:** PASS · 36.9 s · offloads 1 · refused 0 · bits {'hr': 0, 'acme': 9, 'legal': 0} · leaked none

**prompt to the agent:** Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-6aeffb57/acme/contract.md requires for termination. End your reply with just the number.

## Cloud side: Claude Code in the room (everything that crossed the route)

3 model calls through the route; main thread is call 3 (model `claude-opus-5-5`, system prompt 5906 chars, tools: Agent, Bash, Edit, Glob, Grep, ListAgents, Read, ReportFindings, ScheduleWakeup, Skill, ToolSearch, Workflow, Write, DeferredToolPlaceholder, mcp__lego__offload).


#### user
[system-reminder, 565 chars]


Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-6aeffb57/acme/contract.md requires for termination. End your reply with just the number.

#### system
[harness context, 9390 chars, not shown]

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

#### system
[harness context, 79 chars, not shown]

#### assistant
**→ mcp__lego__offload**
```json
{
 "task": "Read the contract and report how many days of notice are required for termination. Answer with the number of days.",
 "inputs": [
  "pop:/tmp/lego-e2e-6aeffb57/acme/contract.md"
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
{"row": "shaped", "answer": 95, "ref": "pop:/tmp/lego-e2e-6aeffb57/out/af1750ae73886936a1d116c614029c96.txt", "allowance_left": {"hr": 16, "acme": 7, "legal": 16}}
```

#### system
[harness context, 119 chars, not shown]

#### assistant
> *thinking:* (redacted)

The contract requires 95 days of notice for termination. The offload tool returned only that number, so I couldn't check the clause text myself. Its full output is saved at `pop:/tmp/lego-e2e-6aeffb57/out/af1750ae73886936a1d116c614029c96.txt`.

95

## Private side: the local model on gpu-box (never left the node)

#### job on `pop`: row **shaped**, scopes ['acme'], 9 bits charged

**prompt** (`deepseek-v4.1-flash-1017`, constrained: True)
```
Read the contract and report how many days of notice are required for termination. Answer with the number of days.

--- /tmp/lego-e2e-6aeffb57/acme/contract.md ---
# Master services agreement ACME-E1DD40
Acme Corp and the Company.

## 9. Termination
Either party may terminate on ninety-five (95) days written notice.

```

**reasoning**
```
We need answer from contract. Need number days. Contract says ninety-five (95) days. Answer with number of days. Should be 95. Need concise.
```

**output** (stays on the node; the cloud gets only the reply row)
```
{"answer": 95}
```

usage: {'prompt_tokens': 129, 'total_tokens': 170, 'completion_tokens': 41, 'prompt_tokens_details': {'cached_tokens': 0, 'created_cache_tokens': 128, 'multimodal_tokens': None}, 'completion_tokens_details': {'reasoning_tokens': 33}}
