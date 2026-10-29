"""Agents: how each coding-agent CLI is pointed at the Route (its model) and the Door (the
offload tool) from a throwaway HOME. No credential is written here: the route strips what
the agent sends and injects the real one, so the room only ever holds placeholders.

Each adapter takes (home, route_url, door_url, args) and returns (argv, install_dir, env).
"""
import base64
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import route

PLACEHOLDER = "room-placeholder"


def _bin(name):
    found = shutil.which(name) or os.path.expanduser(f"~/.local/bin/{name}")
    if not os.path.exists(found):
        raise SystemExit(f"lego: {name} is not installed")
    return os.path.realpath(found)


def claude(home, route_url, door_url, args):
    mcp = Path(home, "mcp.json")
    mcp.write_text(json.dumps({"mcpServers": {"lego": {"type": "http", "url": door_url}}}))
    extra = [] if "--allowedTools" in args else ["--allowedTools", "mcp__lego__offload"]
    b = _bin("claude")
    return [b, *args, "--mcp-config", str(mcp), "--strict-mcp-config", *extra], os.path.dirname(b), {
        "ANTHROPIC_BASE_URL": route_url, "ANTHROPIC_API_KEY": PLACEHOLDER, "CLAUDE_CODE_TMPDIR": f"{home}/tmp",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}


def codex(home, route_url, door_url, args):
    user = Path("~/.codex/config.toml").expanduser()
    cfg = tomllib.loads(user.read_text()) if user.exists() else {}
    keep = [f'{k} = "{cfg[k]}"' for k in ("model", "model_reasoning_effort") if k in cfg]
    Path(home, ".codex").mkdir()
    Path(home, ".codex/config.toml").write_text("\n".join(keep + [
        'model_provider = "lego"', 'sandbox_mode = "danger-full-access"   # the room is the sandbox', "",
        "[model_providers.lego]", 'name = "lego route"', f'base_url = "{route_url}/backend-api/codex"',
        'wire_api = "responses"', 'env_key = "LEGO_ROUTE_KEY"', "", "[mcp_servers.lego]", f'url = "{door_url}"']) + "\n")
    b = _bin("codex")
    return [b, *args], os.path.dirname(os.path.dirname(b)), {"CODEX_HOME": f"{home}/.codex", "LEGO_ROUTE_KEY": PLACEHOLDER}


def cursor(home, route_url, door_url, args):
    d = Path(home, ".cursor")
    d.mkdir()
    cfg = json.loads(Path("~/.cursor/cli-config.json").expanduser().read_text())    # preferences, no secrets
    cfg = {k: v for k, v in cfg.items() if not k.endswith("Cache")}                  # server caches name real hosts
    cfg.update(network={"useHttp1ForAgent": True}, sandbox={"mode": "disabled"})
    (d / "cli-config.json").write_text(json.dumps(cfg))
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {"lego": {"url": door_url}}}))
    head, body, _ = route.keychain("cursor-access-token").split(".")     # the client only decodes the claims;
    decoy = f"{head}.{body}.{base64.urlsafe_b64encode(PLACEHOLDER.encode()).decode().rstrip('=')}"   # route swaps it
    (d / "auth.json").write_text(json.dumps({"accessToken": decoy, "refreshToken": decoy}))
    b = _bin("cursor-agent")
    return [b, *args, "--approve-mcps"], os.path.dirname(b), {"CURSOR_API_ENDPOINT": route_url,
                                                              "AGENT_CLI_CREDENTIAL_STORE": "file"}


def cline(home, route_url, door_url, args):
    pkg = Path(_bin("cline")).parent.parent                              # npm wrapper -> compiled binary
    b = str(next(pkg.glob("node_modules/@cline/cli-darwin-*/bin/cline")).resolve())
    old = Path("~/.cline/data/globalState.json").expanduser()
    model = (json.loads(old.read_text()) if old.exists() else {}).get("actModeOpenRouterModelId") or "moonshotai/kimi-k2"
    s = Path(home, ".cline/data/settings")
    s.mkdir(parents=True)
    (s / "providers.json").write_text(json.dumps({"version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
        "providers": {"openai-compatible": {"settings": {
            "provider": "openai-compatible", "apiKey": PLACEHOLDER, "model": model, "baseUrl": f"{route_url}/api/v1"},
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "tokenSource": "manual"}}}))
    (s / "cline_mcp_settings.json").write_text(json.dumps(
        {"mcpServers": {"lego": {"transport": {"type": "streamableHttp", "url": door_url}}}}))
    return [b, *args], os.path.dirname(b), {"CLINE_NO_AUTO_UPDATE": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}


# name: (adapter, route upstream)
AGENTS = {"claude": (claude, "anthropic"), "codex": (codex, "codex"),
          "cursor": (cursor, "cursor"), "cline": (cline, "openrouter")}


def cline_reply(out):
    for line in reversed(out.splitlines()):
        if line.startswith("{") and '"run_result"' in line:
            return json.loads(line).get("text") or ""
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


# name: (arguments that run one prompt unattended, the final reply from stdout)
ONESHOT = {"claude": (lambda p: ["-p", p, "--output-format", "json", "--dangerously-skip-permissions"],
                      lambda out: json.loads(out).get("result") or ""),
           "codex": (lambda p: ["exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", p],
                     lambda out: out),
           "cursor": (lambda p: ["-p", p, "--yolo", "--trust"], lambda out: out),
           "cline": (lambda p: ["-y", "--json", p], cline_reply)}

