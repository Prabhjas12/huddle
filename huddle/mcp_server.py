"""Huddle's tool connector (Model Context Protocol over stdio).

Started by Claude Code or Codex when you chat on Huddle's website (and by the
Claude desktop app if you connected it). The AI calls Huddle's tools through it.

When the Huddle website is running, tool calls are forwarded to it, so answers
use the league data it already has loaded. Otherwise the connector loads the
data itself.

Written without the MCP SDK so it runs on the Python that ships with macOS (3.9),
and it imports nothing heavy until a tool actually runs, so it starts instantly.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from . import __version__
from .toolspec import CONNECTOR_INSTRUCTIONS, CONNECTOR_TOOLS, TOOLS

# stdout carries the protocol; anything else printed goes to stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

SERVER_URL = os.environ.get("HUDDLE_SERVER_URL", "").rstrip("/")
SERVER_TOKEN = os.environ.get("HUDDLE_TOOL_TOKEN", "")
CTX_MAX_AGE = 30 * 60

_lock = threading.Lock()
_state: Dict[str, Any] = {"ctx": None, "cache": None}


def _log(msg: str) -> None:
    print(f"[huddle] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------- local mode
def _get_ctx(force: bool = False):
    from .cache import HttpCache
    from .config import DATA_DIR, load_settings
    from .model import LeagueContext
    from .sleeper import Sleeper
    with _lock:
        if _state["cache"] is None:
            _state["cache"] = HttpCache(DATA_DIR / "cache")
        cache = _state["cache"]
        ctx = _state["ctx"]
        if ctx is not None and not force and time.time() - ctx.built_at < CTX_MAX_AGE:
            return ctx
        if force:
            cache.clear(max_age=600)
        try:
            ctx = LeagueContext(load_settings(), cache, Sleeper(cache)).build()
        except Exception:
            if _state["ctx"] is not None and not isinstance(_state["ctx"], type(None)):
                return _state["ctx"]
            raise
        _state["ctx"] = ctx
        _log(f"league loaded: {ctx.league_name}, week {ctx.week} ({ctx.build_seconds}s)")
        return ctx


def _run_local(name: str, args: Dict[str, Any]) -> Tuple[str, bool]:
    from .agent import run_tool_text
    return run_tool_text(_get_ctx, name, args)


# ------------------------------------------------------------- proxy mode
def _run_remote(name: str, args: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
    if not SERVER_URL:
        return None
    req = urllib.request.Request(
        SERVER_URL + "/api/tools/call", data=json.dumps({"name": name, "arguments": args}).encode(),
        headers={"Content-Type": "application/json", "X-Huddle-Token": SERVER_TOKEN}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read().decode())
        return data.get("text", ""), bool(data.get("isError"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"Huddle website not reachable ({exc}); loading data locally")
        return None


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        res = _run_remote(name, args) or _run_local(name, args)
        text, is_err = res
    except Exception as exc:
        _log(traceback.format_exc())
        text, is_err = f"Huddle couldn't load data: {exc}", True
    return {"content": [{"type": "text", "text": text}], "isError": is_err}


def tool_list() -> list:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"],
             "annotations": {"readOnlyHint": t["name"] != "refresh_data", "openWorldHint": False}}
            for t in CONNECTOR_TOOLS + TOOLS]


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if mid is None:            # notification (e.g. notifications/initialized): no reply
        return None
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "huddle", "title": "Huddle fantasy football", "version": __version__},
            "instructions": CONNECTOR_INSTRUCTIONS,
        }
        if not SERVER_URL:   # standalone (Claude desktop app): start loading data right away
            threading.Thread(target=lambda: _safe_warm(), daemon=True).start()
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method in ("resources/list", "resources/templates/list"):
        result = {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _safe_warm() -> None:
    try:
        _get_ctx()
    except Exception as exc:
        _log(f"warm-up failed: {exc}")


def _send(obj: Dict[str, Any]) -> None:
    _PROTO_OUT.write(json.dumps(obj, default=str) + "\n")
    _PROTO_OUT.flush()


def main() -> None:
    _log("connector started" + (f" (forwarding to {SERVER_URL})" if SERVER_URL else ""))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            try:
                reply = handle(m)
            except Exception as exc:
                _log(traceback.format_exc())
                reply = {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32603, "message": str(exc)}}
            if reply is not None:
                _send(reply)


if __name__ == "__main__":
    main()
