"""The AI chat: an LLM that answers by calling Huddle's analysis tools.

The model never answers football questions from memory. It calls tools that
read your league and the latest data, then explains the result. Three brains
are supported and switchable in Settings; all of them get exactly the same
tools and data:

  - Claude (Anthropic API key)
  - ChatGPT (OpenAI API key, via the Responses API)
  - Ollama (local model) - free and private, runs on your Mac
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import reports
from .config import anthropic_key, openai_key
from .model import LeagueContext
from .scoring import describe
from .trades import TradeEngine

MAX_TOOL_ROUNDS = 8
MAX_RESULT_CHARS = 14000

from .toolspec import TOOLS, tool_label  # noqa: E402


class ToolRunner:
    def __init__(self, ctx: LeagueContext):
        self.ctx = ctx
        self._engine: Optional[TradeEngine] = None

    @property
    def engine(self) -> TradeEngine:
        if self._engine is None:
            self._engine = TradeEngine(self.ctx)
        return self._engine

    def _pid(self, name: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        hits = self.ctx.search(name, limit=4)
        if not hits:
            return None, {"error": f"No player found matching '{name}'."}
        return hits[0], None

    def _team(self, name: str) -> Optional[int]:
        q = (name or "").lower().strip()
        for rid, t in self.ctx.teams.items():
            if q and (q == t["name"].lower() or q == t["manager"].lower()):
                return rid
        for rid, t in self.ctx.teams.items():
            if q and (q in t["name"].lower() or q in t["manager"].lower()):
                return rid
        return None

    def run(self, name: str, args: Dict[str, Any]) -> Any:
        ctx = self.ctx
        try:
            if name == "get_my_team":
                t = ctx.team()
                return {"team": t["name"], "record": f"{t['wins']}-{t['losses']}-{t['ties']}",
                        "points_for": round(t["fpts"], 1), "roster": reports.roster_view(ctx)}
            if name == "get_player_report":
                pid, err = self._pid(args.get("player", ""))
                return err or reports.player_report(ctx, pid)
            if name == "compare_players":
                pids = []
                for n in (args.get("players") or [])[:5]:
                    pid, err = self._pid(n)
                    if err:
                        return err
                    pids.append(pid)
                return reports.compare(ctx, pids)
            if name == "get_lineup_advice":
                return reports.lineup_advice(ctx, week=args.get("week"))
            if name == "analyze_trade":
                give, get = [], []
                for side, out in (("give", give), ("get", get)):
                    for n in args.get(side) or []:
                        pid, err = self._pid(n)
                        if err:
                            return err
                        out.append(pid)
                return self.engine.analyze(give, get)
            if name == "find_trades":
                partner = self._team(args["partner_team"]) if args.get("partner_team") else None
                return self.engine.find(focus_pos=args.get("position"), partner=partner, max_results=6)
            if name == "get_waiver_targets":
                r = reports.waiver_targets(ctx, position=args.get("position"), limit=6)
                keep = ("name", "pos", "team", "opp", "injury", "proj", "ros_ppg", "net_gain", "this_week_gain",
                        "playoff_gain", "starts", "weeks", "reasons", "bid")
                slim = lambda row: {**{k: row.get(k) for k in keep}, "make_room": row["move"]["text"]}  # noqa: E731
                return {"needs": r["needs"], "best_pickups": [slim(x) for x in r["views"]["best"][:6]],
                        "streamers_this_week": [slim(x) for x in r["views"]["week"][:4]],
                        "stashes": [slim(x) for x in r["views"]["stash"][:4]],
                        "your_drop_list": [{"name": b["name"], "verdict": b["verdict"], "reasons": b["reasons"]}
                                           for b in r["drop_board"][:5]],
                        "faab": r["faab"]}
            if name == "get_defense_vs_position":
                return reports.dvp_table(ctx, args.get("position", "WR"))
            if name == "get_weekly_matchup":
                return reports.weekly_matchup(ctx, week=args.get("week"))
            if name == "get_league_standings":
                return reports.standings(ctx)
            if name == "get_backtest":
                from . import backtest
                return backtest.for_ai(backtest.cached_run(ctx))
            if name == "get_team_roster":
                rid = self._team(args.get("team", ""))
                if rid is None:
                    return {"error": "No team matches that name.",
                            "teams": [t["name"] for t in ctx.teams.values()]}
                return {"team": ctx.teams[rid]["name"], "roster": reports.roster_view(ctx, rid)}
            return {"error": f"Unknown tool {name}"}
        except ValueError as exc:
            return {"error": str(exc)}


def run_tool_text(get_ctx: Callable[..., LeagueContext], name: str, args: Dict[str, Any]) -> Tuple[str, bool]:
    """Run any Huddle tool (including the connector-only ones) and return (text, is_error).

    Used by the connector that Claude Code, Codex and the Claude app talk to.
    """
    from .model import SetupError
    name = (name or "").replace("mcp__huddle__", "")
    try:
        if name == "refresh_data":
            ctx = get_ctx(True)
            return (f"Refreshed. {ctx.league_name}, week {ctx.week}; stats through week "
                    f"{ctx.nfl.last_completed_week}."), False
        ctx = get_ctx(False)
        if name == "get_league_context":
            text = system_prompt(ctx)
            if ctx.warnings:
                text += "\n\nData notes: " + " ".join(ctx.warnings)
            return text, False
        out = ToolRunner(ctx).run(name, args or {})
        is_err = isinstance(out, dict) and "error" in out and len(out) <= 2
        return _clip(out), is_err
    except SetupError as exc:
        return f"{exc} Open Huddle's Settings to finish setup.", True
    except Exception as exc:
        return f"Huddle couldn't load data: {exc}", True


def _clip(obj: Any) -> str:
    text = json.dumps(obj, default=str, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..."(truncated)"'
    return text


def system_prompt(ctx: LeagueContext) -> str:
    me = ctx.team()
    playoff = f"weeks {ctx.playoff_week_start}-{ctx.end_week}"
    prefs = (ctx.settings.get("preferences") or "").strip()
    return f"""You are Huddle, a sharp fantasy football analyst working for one manager.

League: {ctx.league_name} on Sleeper - {ctx.num_teams} teams, {describe(ctx.scoring)}.
Starting lineup: {', '.join(ctx.slots)}. Roster limit {ctx.roster_limit}. Fantasy playoffs: {playoff}.
Manager's team: {me['name']} ({me['wins']}-{me['losses']}{'-' + str(me['ties']) if me['ties'] else ''}).
Current NFL week: {ctx.week} of the {ctx.season} season. Today is {dt.date.today():%A, %B %d, %Y}.

How to work:
- Your own knowledge of players, rosters and injuries is out of date. Use the tools for every fact: call get_player_report for each player you discuss, and analyze_trade for any trade. Never invent stats.
- All points are in this league's scoring. "proj" is this week's projection; "ros_ppg" is rest-of-season points per game.
- Start/sit: weigh projection, matchup (defense vs position), injury/practice status, usage trend, Vegas implied total, home/away and venue. Give a clear call, then the 3-5 numbers that drive it.
- Trades: lead with the lineup impact for the manager (pts/week and in the playoffs), then market value and whether the other side is likely to accept. Point out roster-spot effects and bye-week weeks that get worse.
- Questions about how accurate Huddle is, or whether its lineups would have done better: call get_backtest and report its numbers honestly, including where Huddle did worse.
- Be decisive and concise. Use short bullets. Flag real uncertainty (questionable tags, small samples, committee backfields) in one line.

Manager's standing preferences:
{prefs or '(none set)'}"""


# ------------------------------------------------------------------ providers
def provider_status(settings: Dict[str, Any], provider: Optional[str] = None) -> Dict[str, Any]:
    """Is the chosen AI ready to answer? Includes setup steps when it isn't."""
    from . import cli_agents
    provider = provider or settings.get("ai_provider", "claude")
    if provider == "claude":
        st = cli_agents.claude_status()
        st["model"] = settings.get("claude_sub_model") or ""
    elif provider == "chatgpt":
        st = cli_agents.codex_status()
        st["model"] = settings.get("chatgpt_model") or ""
    elif provider == "ollama":
        ok, models = _ollama_models(settings)
        model = settings.get("ollama_model") or "qwen3:8b"
        have = any(m == model or m.split(":")[0] == model for m in models)
        if not ok:
            st = {"ready": False, "state": "not_running", "message": "Ollama isn't running on this Mac.",
                  "steps": [{"title": "Install Ollama", "command": "", "note": "Download it from ollama.com and open it."},
                            {"title": "Download a model", "command": f"ollama pull {model}",
                             "note": "Run this in Terminal (about 5 GB)."}]}
        elif not have:
            st = {"ready": False, "state": "no_model", "message": f"Ollama is running but doesn't have {model} yet.",
                  "steps": [{"title": "Download the model", "command": f"ollama pull {model}", "note": "Run this in Terminal."}]}
        else:
            st = {"ready": True, "state": "ready", "message": f"Ready: {model} on this Mac."}
        st.update(models=models, model=model)
    else:
        vendor = settings.get("api_vendor", "anthropic")
        key = anthropic_key(settings) if vendor == "anthropic" else openai_key(settings)
        model = settings.get("claude_model") if vendor == "anthropic" else settings.get("openai_model")
        st = {"ready": bool(key), "state": "ready" if key else "no_key", "vendor": vendor, "model": model,
              "message": (f"Ready: {model}." if key else "Paste your API key below.")}
    st["provider"] = provider
    return st


def openai_models(settings: Dict[str, Any]) -> List[str]:
    """Chat models this OpenAI key can use (for the Settings dropdown)."""
    key = openai_key(settings)
    if not key:
        return []
    try:
        r = httpx.get(settings.get("openai_url", "https://api.openai.com/v1").rstrip("/") + "/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=8)
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:
        return []
    skip = ("audio", "realtime", "transcribe", "tts", "image", "embedding", "moderation", "search", "whisper", "dall-e")
    return sorted((i for i in ids if i.startswith(("gpt-", "o")) and not any(x in i for x in skip)), reverse=True)


def _ollama_models(settings: Dict[str, Any]) -> Tuple[bool, List[str]]:
    try:
        r = httpx.get(settings.get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/tags", timeout=1.5)
        r.raise_for_status()
        return True, [m.get("name") for m in r.json().get("models", [])]
    except Exception:
        return False, []


def chat(ctx: LeagueContext, history: List[Dict[str, str]], message: str,
         on_event: Optional[Callable[[Dict[str, Any]], None]] = None, session: Optional[str] = None,
         server_url: str = "", token: str = "", cancel: Any = None) -> Dict[str, Any]:
    """Answer one message. on_event receives {"type": "tool"|"delta"|..., ...} while it works.
    cancel is an optional threading.Event; setting it stops the run (the chat's Stop button)."""
    from . import cli_agents
    emit = on_event or (lambda e: None)
    settings = ctx.settings
    provider = settings.get("ai_provider", "claude")
    if provider in ("claude", "chatgpt"):
        run = cli_agents.run_claude if provider == "claude" else cli_agents.run_codex
        model = settings.get("claude_sub_model" if provider == "claude" else "chatgpt_model") or ""
        res = run(message, system_prompt(ctx), session, model, server_url, token, emit, cancel)
        stale = ("no conversation found", "no rollout found", "session not found")
        if session and res.get("error") and any(x in (res.get("answer") or "").lower() for x in stale):
            res = run(message, system_prompt(ctx), None, model, server_url, token, emit, cancel)   # start fresh
        return res
    runner = ToolRunner(ctx)
    run_tool = runner.run

    def run_and_report(name: str, args: Dict[str, Any]) -> Any:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Stopped")
        try:
            return run_tool(name, args)
        finally:
            emit({"type": "tool_done"})

    runner.run = run_and_report  # type: ignore[assignment]
    on_tool = lambda n, a: emit({"type": "tool", "name": n, "label": tool_label(n, a)})  # noqa: E731
    if provider == "ollama":
        return _chat_ollama(ctx, runner, history, message, on_tool)
    vendor = settings.get("api_vendor", "anthropic")
    if vendor == "openai":
        if not openai_key(settings):
            return {"answer": "Add your OpenAI API key in Settings.", "tools_used": [], "error": True}
        return _chat_openai(ctx, runner, history, message, on_tool)
    if not anthropic_key(settings):
        return {"answer": "Add your Anthropic API key in Settings.", "tools_used": [], "error": True}
    return _chat_claude(ctx, runner, history, message, on_tool)


def _chat_claude(ctx, runner, history, message, on_tool):
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_key(ctx.settings))
    model = ctx.settings.get("claude_model") or "claude-sonnet-5-5"
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOLS]
    messages: List[Dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history[-12:]
                                      if h.get("role") in ("user", "assistant") and h.get("content")]
    messages.append({"role": "user", "content": message})
    used = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = client.messages.create(model=model, max_tokens=2500, system=system_prompt(ctx),
                                          tools=tools, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    used.append({"name": block.name, "input": block.input})
                    if on_tool:
                        on_tool(block.name, block.input)
                    out = runner.run(block.name, dict(block.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": _clip(out)})
            messages.append({"role": "user", "content": results})
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return {"answer": text or "(no answer)", "tools_used": used, "provider": "api", "model": model}
    except anthropic.AuthenticationError:
        return {"answer": "Claude rejected the API key. Check it in Settings.", "tools_used": used, "provider": "claude", "error": True}
    except anthropic.NotFoundError:
        return {"answer": f"Claude model '{model}' wasn't found. Change the model name in Settings.", "tools_used": used,
                "provider": "claude", "error": True}
    except anthropic.APIError as exc:
        return {"answer": f"Claude API error: {exc}", "tools_used": used, "provider": "claude", "error": True}


def _chat_openai(ctx, runner, history, message, on_tool):
    """ChatGPT through OpenAI's Responses API with function calling."""
    base = ctx.settings.get("openai_url", "https://api.openai.com/v1").rstrip("/")
    model = ctx.settings.get("openai_model") or "gpt-6.1-sol"
    headers = {"Authorization": f"Bearer {openai_key(ctx.settings)}"}
    tools = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"]}
             for t in TOOLS]
    items: List[Dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history[-12:]
                                   if h.get("role") in ("user", "assistant") and h.get("content")]
    items.append({"role": "user", "content": message})
    payload: Dict[str, Any] = {"model": model, "instructions": system_prompt(ctx), "tools": tools,
                               "reasoning": {"effort": "low"}, "max_output_tokens": 4000}
    used: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {}
    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as http:
            for _ in range(MAX_TOOL_ROUNDS):
                r = http.post(base + "/responses", headers=headers, json=dict(payload, input=items))
                if r.status_code == 400 and "reasoning" in r.text and "reasoning" in payload:
                    payload.pop("reasoning")          # model without reasoning settings
                    r = http.post(base + "/responses", headers=headers, json=dict(payload, input=items))
                if r.status_code >= 400:
                    return {"answer": _openai_error(r, model), "tools_used": used, "provider": "openai", "error": True}
                data = r.json()
                output = data.get("output") or []
                items += output                      # keep the model's own items (incl. reasoning) in context
                calls = [o for o in output if o.get("type") == "function_call"]
                if not calls:
                    break
                for c in calls:
                    try:
                        args = json.loads(c.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    used.append({"name": c.get("name"), "input": args})
                    if on_tool:
                        on_tool(c.get("name"), args)
                    out = runner.run(c.get("name", ""), args)
                    items.append({"type": "function_call_output", "call_id": c.get("call_id"), "output": _clip(out)})
        text = "\n".join(part.get("text", "") for o in (data.get("output") or []) if o.get("type") == "message"
                         for part in (o.get("content") or []) if part.get("type") == "output_text").strip()
        return {"answer": text or "(no answer)", "tools_used": used, "provider": "api", "model": model}
    except httpx.HTTPError as exc:
        return {"answer": f"Couldn't reach OpenAI: {exc}", "tools_used": used, "provider": "openai", "error": True}


def _openai_error(r: "httpx.Response", model: str) -> str:
    try:
        err = r.json().get("error", {})
    except Exception:
        err = {}
    code, msg = err.get("code") or "", err.get("message") or r.text[:300]
    if r.status_code == 401:
        return "OpenAI rejected the API key. Check it in Settings."
    if code == "insufficient_quota" or (r.status_code == 429 and "quota" in msg):
        return ("Your OpenAI account has no API credit. A ChatGPT Plus subscription doesn't include API usage; "
                "add a payment method or credits at platform.openai.com/settings/organization/billing.")
    if r.status_code == 404 or code == "model_not_found":
        return f"OpenAI model '{model}' isn't available to your key. Pick another model in Settings."
    if r.status_code == 429:
        return "OpenAI is rate-limiting this key right now. Try again in a minute."
    return f"OpenAI error ({r.status_code}): {msg}"


def _chat_ollama(ctx, runner, history, message, on_tool):
    url = ctx.settings.get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/chat"
    model = ctx.settings.get("ollama_model") or "qwen3:8b"
    tools = [{"type": "function", "function": t} for t in TOOLS]
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt(ctx)}]
    messages += [{"role": h["role"], "content": h["content"]} for h in history[-8:]
                 if h.get("role") in ("user", "assistant") and h.get("content")]
    messages.append({"role": "user", "content": message})
    used = []
    payload: Dict[str, Any] = {"model": model, "tools": tools, "stream": False, "options": {"num_ctx": 16384}}
    if re.search(r"qwen3|deepseek-r1|gpt-oss", model):
        payload["think"] = False
    try:
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=5.0)) as http:
            for _ in range(MAX_TOOL_ROUNDS):
                r = http.post(url, json=dict(payload, messages=messages))
                if r.status_code == 400 and "think" in payload:
                    payload.pop("think")         # older Ollama without the think flag
                    r = http.post(url, json=dict(payload, messages=messages))
                if r.status_code == 404:
                    return {"answer": f"Ollama doesn't have '{model}'. Run: ollama pull {model}", "tools_used": used,
                            "provider": "ollama", "error": True}
                r.raise_for_status()
                msg = r.json().get("message", {})
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    break
                for c in calls:
                    fn = c.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    used.append({"name": fn.get("name"), "input": args})
                    if on_tool:
                        on_tool(fn.get("name"), args)
                    out = runner.run(fn.get("name", ""), args)
                    messages.append({"role": "tool", "content": _clip(out), "tool_name": fn.get("name")})
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        return {"answer": text or "(no answer)", "tools_used": used, "provider": "ollama", "model": model}
    except httpx.HTTPError as exc:
        return {"answer": f"Couldn't reach Ollama at {url}: {exc}", "tools_used": used, "provider": "ollama", "error": True}
