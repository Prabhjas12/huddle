"""Local web server. Run with:  python -m huddle  (or ./run.sh)"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, reports
from .cache import HttpCache
from .config import DATA_DIR, ROOT, load_settings, public_settings, save_settings
from .model import LeagueContext, SetupError
from .sleeper import Sleeper
from .trades import TradeEngine

WEB = ROOT / "web"
CTX_MAX_AGE = 30 * 60

app = FastAPI(title="Huddle", docs_url=None, redoc_url=None)
TOOL_TOKEN = secrets.token_urlsafe(24)   # lets the AI's connector call back into this server
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

_cache = HttpCache(DATA_DIR / "cache")
_lock = threading.Lock()
_state: Dict[str, Any] = {"ctx": None, "engine": None, "error": None, "finder": {}}


def get_ctx(force: bool = False) -> LeagueContext:
    with _lock:
        ctx = _state["ctx"]
        if ctx is not None and not force and time.time() - ctx.built_at < CTX_MAX_AGE:
            return ctx
        settings = load_settings()
        try:
            ctx = LeagueContext(settings, _cache, Sleeper(_cache)).build()
        except SetupError as exc:
            _state.update(ctx=None, engine=None, error=str(exc))
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:  # network etc.
            if _state["ctx"] is not None:   # keep serving the last good data
                return _state["ctx"]
            _state.update(error=f"Couldn't load data: {exc}")
            raise HTTPException(status_code=503, detail=f"Couldn't load data: {exc}")
        _state.update(ctx=ctx, engine=None, error=None, finder={})
        return ctx


def get_engine() -> TradeEngine:
    ctx = get_ctx()
    if _state["engine"] is None or _state["engine"].ctx is not ctx:
        _state["engine"] = TradeEngine(ctx)
    return _state["engine"]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    sleeper_username: Optional[str] = None
    league_id: Optional[str] = None
    ai_provider: Optional[str] = None
    claude_sub_model: Optional[str] = None
    chatgpt_model: Optional[str] = None
    api_vendor: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    claude_model: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    clear_openai_key: Optional[bool] = None
    ollama_url: Optional[str] = None
    ollama_model: Optional[str] = None
    projection_blend: Optional[float] = None
    playoff_weight: Optional[float] = None
    preferences: Optional[str] = None
    theme: Optional[str] = None
    clear_api_key: Optional[bool] = None


@app.get("/api/settings")
def api_settings() -> Dict[str, Any]:
    return {"settings": public_settings(load_settings())}


@app.get("/api/ai/status")
def api_ai_status(provider: Optional[str] = None) -> Dict[str, Any]:
    return agent.provider_status(load_settings(), provider)


@app.post("/api/settings")
def api_save_settings(body: SettingsIn) -> Dict[str, Any]:
    raw = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    updates = {k: v for k, v in raw.items() if v is not None and k not in ("clear_api_key", "clear_openai_key")}
    for k in ("anthropic_api_key", "openai_api_key"):
        if updates.get(k) == "":
            updates.pop(k)                   # blank field = keep the saved key
    if body.clear_api_key:
        updates["anthropic_api_key"] = ""
    if body.clear_openai_key:
        updates["openai_api_key"] = ""
    old = load_settings()
    s = save_settings(updates)
    if any(old.get(k) != s.get(k) for k in ("sleeper_username", "league_id", "projection_blend", "playoff_weight")):
        with _lock:
            _state.update(ctx=None, engine=None, finder={})
    elif _state["ctx"] is not None:
        _state["ctx"].settings = s           # AI settings apply immediately
    return {"settings": public_settings(s)}


@app.get("/api/openai/models")
def api_openai_models() -> List[str]:
    return agent.openai_models(load_settings())


@app.get("/api/leagues")
def api_leagues(username: str) -> List[Dict[str, Any]]:
    sl = Sleeper(_cache)
    user = sl.user(username)
    if not user:
        raise HTTPException(404, f"No Sleeper user called '{username}'.")
    state = sl.state()
    season = state.get("league_season") or state.get("season")
    return [{"league_id": lg["league_id"], "name": lg.get("name"), "season": lg.get("season"),
             "teams": lg.get("total_rosters")} for lg in sl.leagues(user["user_id"], str(season))]


# ------------------------------------------------------------------ data
@app.post("/api/refresh")
def api_refresh() -> Dict[str, Any]:
    _cache.clear(max_age=600)       # refetch anything older than 10 minutes
    ctx = get_ctx(force=True)
    return {"ok": True, "built_seconds": ctx.build_seconds}


@app.get("/api/overview")
def api_overview() -> Dict[str, Any]:
    ctx = get_ctx()
    me = ctx.team()
    return {
        "league": {"id": ctx.league_id, "name": ctx.league_name, "season": ctx.season, "week": ctx.week,
                   "teams": ctx.num_teams, "slots": ctx.slots, "roster_limit": ctx.roster_limit,
                   "playoff_week_start": ctx.playoff_week_start, "end_week": ctx.end_week,
                   "ros_weeks": ctx.ros_weeks, "scoring": agent.describe(ctx.scoring)},
        "team": {"roster_id": me["roster_id"], "name": me["name"], "wins": me["wins"], "losses": me["losses"],
                 "ties": me["ties"], "fpts": round(me["fpts"], 1)},
        "teams": [{"roster_id": t["roster_id"], "name": t["name"], "manager": t["manager"]} for t in ctx.teams.values()],
        "ai_provider": ctx.settings.get("ai_provider", "claude"),
        "theme": ctx.settings.get("theme", "field"),
        "data": {"built_at": ctx.built_at, "build_seconds": ctx.build_seconds, "warnings": ctx.warnings,
                 "stats_through_week": ctx.nfl.last_completed_week,
                 "sleeper_projection_weeks": ctx.sleeper_projection_weeks},
    }


@app.get("/api/lineup")
def api_lineup(week: Optional[int] = None) -> Dict[str, Any]:
    return reports.lineup_advice(get_ctx(), week=week)


@app.get("/api/roster")
def api_roster(roster_id: Optional[int] = None) -> List[Dict[str, Any]]:
    return reports.roster_view(get_ctx(), roster_id)


@app.get("/api/matchup")
def api_matchup(week: Optional[int] = None) -> Dict[str, Any]:
    return reports.weekly_matchup(get_ctx(), week=week)


@app.get("/api/backtest")
def api_backtest(refresh: bool = False) -> Dict[str, Any]:
    """Replay the season so far: how accurate Huddle's projections were, and what its lineups would have scored."""
    from . import backtest
    return backtest.cached_run(get_ctx(), force=refresh)


@app.get("/api/standings")
def api_standings() -> List[Dict[str, Any]]:
    return reports.standings(get_ctx())


@app.get("/api/dvp")
def api_dvp(position: str = "WR") -> List[Dict[str, Any]]:
    return reports.dvp_table(get_ctx(), position)


@app.get("/api/waivers")
def api_waivers(position: Optional[str] = None) -> Dict[str, Any]:
    return reports.waiver_targets(get_ctx(), position=position or None, limit=20)


@app.get("/api/players/search")
def api_search(q: str, scope: str = "all") -> List[Dict[str, Any]]:
    ctx = get_ctx()
    pool = None
    if scope == "mine":
        pool = ctx.team()["players"]
    elif scope == "others":
        pool = [p for p, rid in ctx.owner_of.items() if rid != ctx.my_roster_id]
    pool = [p for p in pool if p in ctx.players] if pool is not None else None
    return [ctx.brief(p) for p in ctx.search(q, limit=8, pool=pool)]


@app.get("/api/player/{pid}")
def api_player(pid: str) -> Dict[str, Any]:
    ctx = get_ctx()
    if pid not in ctx.players:
        raise HTTPException(404, "Unknown player")
    return reports.player_report(ctx, pid)


class TradeIn(BaseModel):
    give: List[str]
    get: List[str]


@app.post("/api/trade/analyze")
def api_trade(body: TradeIn) -> Dict[str, Any]:
    if not body.give or not body.get:
        raise HTTPException(400, "Add at least one player to each side.")
    try:
        return get_engine().analyze(body.give, body.get)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/trade/find")
def api_find(position: Optional[str] = None, partner: Optional[int] = None) -> List[Dict[str, Any]]:
    key = (position or "", partner or 0)
    ctx = get_ctx()
    cached = _state["finder"].get(key)
    if cached and cached[0] == ctx.built_at:
        return cached[1]
    res = get_engine().find(focus_pos=position or None, partner=partner, max_results=10)
    _state["finder"][key] = (ctx.built_at, res)
    return res


# ------------------------------------------------------------------ chat
class ChatIn(BaseModel):
    message: str
    history: List[Dict[str, str]] = []
    session: Optional[str] = None
    chat_id: Optional[str] = None


_RUNNING: Dict[str, threading.Event] = {}   # chat_id -> stop flag, for answers still being written


@app.post("/api/chat")
def api_chat(body: ChatIn, request: Request) -> StreamingResponse:
    ctx = get_ctx()
    server_url = f"http://127.0.0.1:{request.url.port or 8765}"
    q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    cancel = threading.Event()
    if body.chat_id:
        _RUNNING[body.chat_id] = cancel

    def work() -> None:
        try:
            res = agent.chat(ctx, body.history, body.message, on_event=q.put, session=body.session,
                             server_url=server_url, token=TOOL_TOKEN, cancel=cancel)
            q.put({"type": "answer", **res})
        except Exception as exc:
            q.put({"type": "answer", "answer": f"Something went wrong: {exc}", "error": True, "tools_used": []})
        finally:
            _RUNNING.pop(body.chat_id or "", None)
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class CancelIn(BaseModel):
    chat_id: str


@app.post("/api/chat/cancel")
def api_chat_cancel(body: CancelIn) -> Dict[str, Any]:
    """The Stop button. Ends the AI run so it doesn't keep going in the background."""
    ev = _RUNNING.get(body.chat_id)
    if ev:
        ev.set()
    return {"stopped": bool(ev)}


class ToolCallIn(BaseModel):
    name: str
    arguments: Dict[str, Any] = {}


@app.post("/api/tools/call")
def api_tool_call(body: ToolCallIn, x_huddle_token: str = Header("")) -> Dict[str, Any]:
    """Called by the connector that Claude Code / Codex start, so tools use this server's loaded data."""
    if not secrets.compare_digest(x_huddle_token or "", TOOL_TOKEN):
        raise HTTPException(403, "Bad token")

    def ctx_getter(force: bool = False) -> LeagueContext:
        if force:
            _cache.clear(max_age=600)
        try:
            return get_ctx(force=force)
        except HTTPException as exc:
            raise SetupError(str(exc.detail))

    text, is_err = agent.run_tool_text(ctx_getter, body.name, body.arguments)
    return {"text": text, "isError": is_err}


@app.exception_handler(HTTPException)
def http_error(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
