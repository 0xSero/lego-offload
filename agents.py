"""Agents: how each coding-agent CLI in the VM is pointed at the Route (its model) and the Door
(the lego tool) from a throwaway HOME. No credential is written here: the agent holds only the
session's key, and the route injects the real credential on the Mac.

Each adapter takes (home, route_url, door_url, args, ws, key), omp also a model, and returns
(argv, env).
"""
import json
import os
from pathlib import Path


def claude(home, route_url, door_url, args, ws, key):
    mcp = Path(home, "mcp.json")
    mcp.write_text(json.dumps({"mcpServers": {"lego": {"type": "http", "url": door_url,
                                                       "headers": {"Authorization": f"Bearer {key}"}}}}))
    extra = [] if "--allowedTools" in args else ["--allowedTools", "mcp__lego__offload"]
    Path(home, ".claude.json").write_text(json.dumps({                 # skip onboarding: it dials api.anthropic.com
        "hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True,
        "projects": {os.path.realpath(ws): {"hasTrustDialogAccepted": True}},
        "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []}}))
    return ["claude", *args, "--mcp-config", str(mcp), "--strict-mcp-config", *extra], {
        "ANTHROPIC_BASE_URL": route_url, "ANTHROPIC_API_KEY": key, "CLAUDE_CODE_TMPDIR": f"{home}/tmp",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
        "MCP_TOOL_TIMEOUT": "900000"}


def omp(home, route_url, door_url, args, ws, key, model):
    """oh-my-pi on a local model: the route forwards to the model's URL."""
    d = Path(home, ".omp/agent")
    d.mkdir(parents=True)
    (d / "models.yml").write_text(json.dumps({"providers": {"lego": {
        "baseUrl": f"{route_url}/v1", "apiKey": key, "api": "openai-completions", "models": [{
            "id": model["model"], "name": model["model"], "reasoning": True, "input": ["text"],
            "contextWindow": model.get("context", 262144), "maxTokens": model.get("context", 262144),
            "omitMaxOutputTokens": True, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
    (d / "config.yml").write_text(json.dumps({"modelRoles": {"default": f"lego/{model['model']}"},
                                              "startup": {"quiet": True, "setupWizard": False, "checkUpdate": False}}))
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {"lego": {"type": "http", "url": door_url, "timeout": 900000,
                                                                     "headers": {"Authorization": f"Bearer {key}"}}}}))
    return ["omp", *args], {}


# name: (adapter, route upstream; None: the agent runs on a local model, the route goes to its URL)
AGENTS = {"claude": (claude, "anthropic"), "omp": (omp, None)}


# name: (arguments that run one prompt unattended, the final reply from stdout)
ONESHOT = {"claude": (lambda p: ["-p", p, "--output-format", "json", "--dangerously-skip-permissions"],
                      lambda out: json.loads(out).get("result") or ""),
           "omp": (lambda p: ["-p", "--no-session", "--approval-mode", "yolo", "--no-title", p], lambda out: out)}

