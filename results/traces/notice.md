# notice

**result:** PASS · 40.9 s · offloads 1 · refused 0 · bits {'hr': 0, 'acme': 9, 'legal': 0} · leaked none

**prompt to the agent:** Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-c8a8e249/acme/contract.md requires for termination. End your reply with just the number.

## Cloud side: Claude Code in the room (everything that crossed the route)

3 model calls through the route; main thread is call 3 (model `claude-opus-5-5`, system prompt 5906 chars, tools: Agent, Bash, Edit, Glob, Grep, ListAgents, Read, ReportFindings, ScheduleWakeup, Skill, ToolSearch, Workflow, Write, DeferredToolPlaceholder, mcp__lego__offload).


#### user
[system-reminder, 565 chars]


Using the offload tool, find how many days of notice pop:/tmp/lego-e2e-c8a8e249/acme/contract.md requires for termination. End your reply with just the number.

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
 "task": "Read the contract and report how many days of notice are required for termination. Answer with the integer number of days.",
 "inputs": [
  "pop:/tmp/lego-e2e-c8a8e249/acme/contract.md"
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
{"row": "shaped", "answer": 95, "ref": "pop:/tmp/lego-e2e-c8a8e249/out/346ad5170c055e44eb7eef748a9d30fd.txt", "allowance_left": {"hr": 16, "acme": 7, "legal": 16}}
```

#### system
[harness context, 119 chars, not shown]

#### assistant
> *thinking:* (redacted)

I gave the contract to the offload tool, and it says termination requires 95 days' notice. I only got the number back, not the clause, so I haven't checked the wording myself. 95 is an unusual figure, so it's worth confirming against the contract text. The full output is saved at `pop:/tmp/lego-e2e-c8a8e249/out/346ad5170c055e44eb7eef748a9d30fd.txt`.

95

## Private side: the local model on gpu-box (never left the node)

#### job on `pop`: row **shaped**, scopes ['acme'], 9 bits charged

**prompt** (`deepseek-v4.1-flash-1017`, constrained: True)
```
Read the contract and report how many days of notice are required for termination. Answer with the integer number of days.

--- /tmp/lego-e2e-c8a8e249/acme/contract.md ---
# Master services agreement ACME-CE3175
Acme Corp and the Company.

## 9. Termination
Either party may terminate on ninety-five (95) days written notice.

```

**reasoning**
```
We need answer integer days. Need read contract. It says ninety-five (95) days. Answer integer 95. Need just answer.
```

**output** (stays on the node; the cloud gets only the reply row)
```
{  "answer": 95 }
```

usage: {'prompt_tokens': 130, 'total_tokens': 168, 'completion_tokens': 38, 'prompt_tokens_details': {'cached_tokens': 0, 'created_cache_tokens': 128, 'multimodal_tokens': None}, 'completion_tokens_details': {'reasoning_tokens': 28}}
