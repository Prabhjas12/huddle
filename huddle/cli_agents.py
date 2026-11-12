"""Run Ask Huddle on your Claude or ChatGPT *subscription* - no API key.

Claude:  Claude Code (Anthropic's command-line app), signed in with your Claude account,
         run headless:  claude -p ... --output-format stream-json
ChatGPT: Codex (OpenAI's command-line app), signed in with your ChatGPT account,
         run headless:  codex exec --json ...

Both are given only Huddle's tools (through mcp_server.py, which forwards to this
running website), a fantasy-analyst system prompt, and an empty working folder.
Built-in tools that could touch your files are switched off.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import DATA_DIR, ROOT
from .toolspec import tool_label

TIMEOUT = 300
WORKDIR = DATA_DIR / "assistant"

_EXTRA_DIRS = [
    "~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "~/.volta/bin", "~/.bun/bin",
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
]

CLAUDE_MODELS = [("", "Your plan's default"), ("sonnet", "Sonnet (fast)"), ("opus", "Opus (deepest)")]


def _search_path() -> str:
    extra = [os.path.expanduser(d) for d in _EXTRA_DIRS]
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.exists():
        extra += [str(p / "bin") for p in sorted(nvm.iterdir(), reverse=True)]
    parts = os.environ.get("PATH", "").split(os.pathsep) + extra
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return os.pathsep.join(out)


def find(binary: str) -> Optional[str]:
    return shutil.which(binary, path=_search_path())


def _env(drop: List[str]) -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = _search_path()
    for k in drop:
        env.pop(k, None)
    return env


def _quick(cmd: List[str], env: Dict[str, str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL, cwd=str(_workdir()))


def _workdir() -> Path:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    return WORKDIR


def mcp_server_spec(server_url: str, token: str) -> Dict[str, Any]:
    return {"command": sys.executable, "args": ["-m", "huddle.mcp_server"],
            "env": {"PYTHONPATH": str(ROOT), "HUDDLE_SERVER_URL": server_url, "HUDDLE_TOOL_TOKEN": token,
                    "HUDDLE_DATA_DIR": str(DATA_DIR)}}


# ===================================================================== status
CLAUDE_STEPS = {
    "install": {"title": "Install Claude Code", "command": "curl -fsSL https://claude.ai/install.sh | bash",
                "note": "Paste this into the Terminal app. It's Anthropic's official command-line Claude."},
    "login": {"title": "Sign in with your Claude account", "command": "claude auth login",
              "note": "Run this in Terminal. Your browser opens; sign in with the account that has your subscription."},
}
CODEX_STEPS = {
    "install": {"title": "Install Codex", "command": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                "note": "Paste this into the Terminal app. Codex is OpenAI's official command-line ChatGPT."},
    "login": {"title": "Sign in with your ChatGPT account", "command": "codex login",
              "note": "Run this in Terminal and choose Sign in with ChatGPT."},
}


def claude_status() -> Dict[str, Any]:
    exe = find("claude")
    if not exe:
        return {"ready": False, "state": "not_installed", "message": "Claude Code isn't installed on this Mac yet.",
                "steps": [CLAUDE_STEPS["install"], CLAUDE_STEPS["login"]]}
    env = _env(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    try:
        out = _quick([exe, "auth", "status"], env)
        info = json.loads(out.stdout or "{}")
    except Exception:
        info = {}
    if not info.get("loggedIn"):
        return {"ready": False, "state": "signed_out", "message": "Claude Code is installed but not signed in.",
                "steps": [CLAUDE_STEPS["login"]], "path": exe}
    method = str(info.get("authMethod") or "")
    plan = info.get("subscriptionType") or info.get("subscription") or ""
    if "console" in method.lower() or "api" in method.lower():
        return {"ready": True, "state": "api_login", "path": exe,
                "message": "Signed in with an Anthropic Console account, so usage is billed to that account. "
                           "Run claude auth login to switch to your Claude subscription."}
    who = info.get("email") or info.get("account") or ""
    return {"ready": True, "state": "ready", "path": exe,
            "message": "Ready" + (f": signed in as {who}" if who else "") + (f" ({plan} plan)" if plan else "") + "."}


def codex_status() -> Dict[str, Any]:
    exe = find("codex")
    if not exe:
        return {"ready": False, "state": "not_installed", "message": "Codex isn't installed on this Mac yet.",
                "steps": [CODEX_STEPS["install"], CODEX_STEPS["login"]]}
    try:
        out = _quick([exe, "login", "status"], _env(["OPENAI_API_KEY", "CODEX_API_KEY"]))
        text = (out.stdout + out.stderr).strip()
    except Exception as exc:
        text = str(exc)
    low = text.lower()
    if "not logged in" in low or not low:
        return {"ready": False, "state": "signed_out", "message": "Codex is installed but not signed in.",
                "steps": [CODEX_STEPS["login"]], "path": exe}
    if "api key" in low:
        return {"ready": True, "state": "api_login", "path": exe,
                "message": "Codex is signed in with an API key, so usage is billed to it. "
                           "Run codex login and choose ChatGPT to use your subscription instead."}
    return {"ready": True, "state": "ready", "path": exe, "message": "Ready: " + text.splitlines()[0] + "."}


# ===================================================================== runners
def _stream(cmd: List[str], env: Dict[str, str], on_line: Callable[[Dict[str, Any]], None],
            cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, env=env, cwd=str(_workdir()), bufsize=1)
    err_lines: List[str] = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr.readlines()), daemon=True)
    t.start()
    if cancel is not None:
        def watch() -> None:   # the Stop button in the chat ends the run so it stops using the plan
            while proc.poll() is None:
                if cancel.wait(0.5):
                    proc.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
    timer = threading.Timer(TIMEOUT, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                on_line(json.loads(line))
            except json.JSONDecodeError:
                continue
        proc.wait()
    finally:
        timer.cancel()
    t.join(timeout=2)
    return {"code": proc.returncode, "stderr": "".join(err_lines)[-2000:]}


def run_claude(message: str, system: str, session: Optional[str], model: str, server_url: str, token: str,
               on_event: Callable[[Dict[str, Any]], None], cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    exe = find("claude")
    if not exe:
        return {"answer": "Claude Code isn't installed yet. Open Settings to set it up.", "error": True}
    mcp = json.dumps({"mcpServers": {"huddle": mcp_server_spec(server_url, token)}})
    cmd = [exe, "-p", message, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--mcp-config", mcp, "--strict-mcp-config", "--tools", "", "--allowedTools", "mcp__huddle",
           "--permission-prompts", "none", "--disable-slash-commands", "--system-prompt", system]
    if model:
        cmd += ["--model", model]
    if session:
        cmd += ["--resume", session]
    st: Dict[str, Any] = {"answer": "", "session": session, "tools": [], "error": False, "model": None}

    def on_line(ev: Dict[str, Any]) -> None:
        typ = ev.get("type")
        if ev.get("session_id"):
            st["session"] = ev["session_id"]
        if typ == "system" and ev.get("subtype") == "init":
            st["model"] = ev.get("model")
        elif typ == "stream_event":
            e = ev.get("event") or {}
            d = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and d.get("type") == "text_delta" and d.get("text"):
                on_event({"type": "delta", "text": d["text"]})
            elif e.get("type") == "message_start":
                on_event({"type": "message_start"})
            elif e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "thinking":
                on_event({"type": "status", "text": "Thinking"})
        elif typ == "user":
            blocks = (ev.get("message") or {}).get("content") or []
            if isinstance(blocks, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                on_event({"type": "tool_done"})
        elif typ == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    name = (block.get("name") or "").replace("mcp__huddle__", "")
                    st["tools"].append({"name": name, "input": block.get("input")})
                    on_event({"type": "tool", "name": name, "label": tool_label(name, block.get("input") or {})})
            if ev.get("error"):
                st["error_kind"] = ev.get("error")
        elif typ == "result":
            st["answer"] = ev.get("result") or ""
            st["error"] = bool(ev.get("is_error"))

    res = _stream(cmd, _env(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]), on_line, cancel)
    if st.get("error_kind") == "authentication_failed" or "authentication" in st["answer"].lower()[:80]:
        st.update(error=True, answer="Claude Code isn't signed in to your Claude account. Open Settings to sign in.")
    elif st.get("error_kind") == "rate_limit" or "usage limit" in st["answer"].lower():
        st["error"] = True
    elif not st["answer"]:
        st.update(error=True, answer="Claude didn't return an answer. " + (res["stderr"].strip().splitlines() or [""])[-1])
    return {"answer": st["answer"], "session": st["session"], "tools_used": st["tools"], "error": st["error"],
            "provider": "claude", "model": st["model"] or (model or "default")}


def run_codex(message: str, system: str, session: Optional[str], model: str, server_url: str, token: str,
              on_event: Callable[[Dict[str, Any]], None], cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    exe = find("codex")
    if not exe:
        return {"answer": "Codex isn't installed yet. Open Settings to set it up.", "error": True}
    spec = mcp_server_spec(server_url, token)
    env_toml = "{" + ",".join(f"{k}={json.dumps(v)}" for k, v in spec["env"].items()) + "}"
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--ignore-user-config",
           "-C", str(_workdir()),
           "-c", f"mcp_servers.huddle.command={json.dumps(spec['command'])}",
           "-c", f"mcp_servers.huddle.args={json.dumps(spec['args'])}",
           "-c", f"mcp_servers.huddle.env={env_toml}",
           "-c", "mcp_servers.huddle.startup_timeout_sec=60",
           "-c", "mcp_servers.huddle.tool_timeout_sec=240",
           "-c", 'approval_policy="never"',
           "-c", f"developer_instructions={json.dumps(system)}"]
    for feature in ("browser_use", "computer_use", "apps"):
        cmd += ["--disable", feature]
    if model:
        cmd += ["-m", model]
    if session:
        cmd += ["resume", session, message]
    else:
        cmd += [message]
    st: Dict[str, Any] = {"answer": "", "session": session, "tools": [], "error": False, "fail": ""}

    def on_line(ev: Dict[str, Any]) -> None:
        typ = ev.get("type")
        if typ == "thread.started":
            st["session"] = ev.get("thread_id") or st["session"]
        item = ev.get("item") or {}
        itype = item.get("type") or item.get("item_type")
        if typ == "item.started" and itype == "mcp_tool_call":
            name = item.get("tool", "")
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            st["tools"].append({"name": name, "input": args})
            on_event({"type": "tool", "name": name, "label": tool_label(name, args)})
        elif typ == "item.completed" and itype == "mcp_tool_call":
            on_event({"type": "tool_done"})
        elif typ == "item.started" and itype == "reasoning":
            on_event({"type": "status", "text": "Thinking"})
        elif typ == "item.completed" and itype in ("agent_message", "assistant_message"):
            st["answer"] = item.get("text") or st["answer"]
        elif typ == "turn.failed":
            st["fail"] = ((ev.get("error") or {}).get("message")) or "Codex stopped with an error."
        elif typ == "error" and not str(ev.get("message", "")).startswith("Reconnecting"):
            st["fail"] = ev.get("message") or st["fail"]

    res = _stream(cmd, _env(["OPENAI_API_KEY", "CODEX_API_KEY"]), on_line, cancel)
    if not st["answer"]:
        fail = st["fail"] or (res["stderr"].strip().splitlines() or ["Codex didn't return an answer."])[-1]
        if "401" in fail or "unauthorized" in fail.lower() or "log in" in fail.lower() or "login" in fail.lower():
            fail = "Codex isn't signed in to your ChatGPT account. Open Settings to sign in."
        return {"answer": fail, "session": st["session"], "tools_used": st["tools"], "error": True,
                "provider": "chatgpt", "model": model or "default"}
    return {"answer": st["answer"], "session": st["session"], "tools_used": st["tools"], "error": False,
            "provider": "chatgpt", "model": model or "default"}
