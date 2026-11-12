"""Player research, lineup advice, waivers, matchups and defense-vs-position."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from .lineup import SLOT_ELIGIBILITY, Lineup, season_totals
from .model import LeagueContext
from .nflverse import norm_name
from .trades import TradeEngine
from .teams import TEAM_NAMES


def _avg(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


# ------------------------------------------------------------- player report
def player_report(ctx: LeagueContext, pid: str) -> Dict[str, Any]:
    p = ctx.players[pid]
    rows = ctx.history.get(pid) or []
    nfl = ctx.nfl
    pos = p["pos"]

    # snaps
    snaps = nfl.snaps_by_pfr.get(p["pfr_id"]) if p.get("pfr_id") else None
    if not snaps:
        snaps = nfl.snaps_by_name.get((norm_name(p["name"]), p["team"])) or {}

    log = []
    for r in rows:
        s = r.get("stats", {})
        g = nfl.game_by_id.get(r.get("game_id") or "") or {}
        tt = nfl.team_totals.get((r.get("team"), r["week"]), {})
        entry = {"week": r["week"], "opp": r.get("opp"), "pts": r["pts"]}
        if g:
            entry["home_away"] = "home" if g.get("home") == r.get("team") else "away"
        if pos != "DEF":
            entry.update({
                "snap_pct": round(100 * snaps[r["week"]], 0) if r["week"] in snaps else None,
                "targets": s.get("rec_tgt", 0), "rec": s.get("rec", 0), "rec_yd": s.get("rec_yd", 0),
                "carries": s.get("rush_att", 0), "rush_yd": s.get("rush_yd", 0),
                "td": s.get("rush_td", 0) + s.get("rec_td", 0) + s.get("pass_td", 0),
                "target_share": round(100 * r["target_share"], 1) if r.get("target_share") is not None else None,
                "carry_share": round(100 * s.get("rush_att", 0) / tt["rush_att"], 1) if tt.get("rush_att") else None,
                "wopr": round(r["wopr"], 2) if r.get("wopr") is not None else None,
            })
            if pos == "QB":
                entry.update({"pass_yd": s.get("pass_yd", 0), "pass_td": s.get("pass_td", 0),
                              "int": s.get("pass_int", 0)})
            if pos == "K":
                entry.update({"fgm": s.get("fgm", 0), "fga": s.get("fga", 0), "xpm": s.get("xpm", 0)})
        else:
            st = r.get("stats", {})
            entry.update({"sacks": st.get("sack", 0), "ints": st.get("int", 0), "fum_rec": st.get("fum_rec", 0),
                          "pts_allowed": st.get("pts_allow", 0), "def_td": st.get("def_td", 0)})
        log.append(entry)

    last3 = log[-3:]
    season_ppg = _avg([g["pts"] for g in log])
    last3_ppg = _avg([g["pts"] for g in last3])
    trend = None
    if season_ppg is not None and last3_ppg is not None and len(log) >= 4:
        d = last3_ppg - season_ppg
        trend = "heating up" if d > 0.15 * max(season_ppg, 4) else ("cooling off" if d < -0.15 * max(season_ppg, 4) else "steady")

    def usage(games: List[Dict[str, Any]]) -> Dict[str, Any]:
        if pos in ("DEF", "K") or not games:
            return {}
        return {
            "snap_pct": _avg([g.get("snap_pct") for g in games]),
            "targets_pg": _avg([g.get("targets") for g in games]),
            "target_share": _avg([g.get("target_share") for g in games]),
            "carries_pg": _avg([g.get("carries") for g in games]),
            "carry_share": _avg([g.get("carry_share") for g in games]),
            "wopr": _avg([g.get("wopr") for g in games]),
        }

    home = [g["pts"] for g in log if g.get("home_away") == "home"]
    away = [g["pts"] for g in log if g.get("home_away") == "away"]

    # next game + environment
    g = nfl.game(p["team"], ctx.week)
    next_game = None
    if g:
        m = ctx.matchup(g["opp"], pos)
        next_game = {
            "week": ctx.week, "opponent": f"{TEAM_NAMES.get(g['opp'], g['opp'])} ({g['opp']})",
            "home_away": g["home_away"], "kickoff": f"{g['weekday']} {g['gameday']} {g['gametime'] or ''} ET".strip(),
            "stadium": g["stadium"], "roof": g["roof"], "surface": g["surface"],
            "weather": ({"temp_f": g["temp"], "wind_mph": g["wind"]} if g.get("temp") is not None else None),
            "vegas": {"total": g["total"], "spread": g["spread"], "implied_team_total": g["implied_total"],
                      "opp_implied_total": g["opp_implied_total"]},
            "divisional": g["div_game"],
            "defense_vs_position": m and {
                "allowed_ppg": m["allowed_ppg"], "league_avg": m["league_avg"], "rank_most_allowed": m["rank"],
                "label": m["label"]},
        }
    elif ctx.nfl.is_bye(p["team"], ctx.week):
        next_game = {"week": ctx.week, "bye": True}

    # upcoming schedule strength
    upcoming = []
    for w in ctx.ros_weeks[1:5]:
        gg = nfl.game(p["team"], w)
        if not gg:
            upcoming.append({"week": w, "opp": "BYE"})
            continue
        mm = ctx.matchup(gg["opp"], pos)
        upcoming.append({"week": w, "opp": ("vs " if gg["home_away"] != "away" else "@ ") + gg["opp"],
                         "matchup": mm["label"] if mm else None, "proj": round(ctx.week_proj(pid, w), 1)})

    inj = nfl.injuries.get(p["gsis_id"]) if p.get("gsis_id") else None
    detail = ctx.proj_detail.get(pid, {}).get(ctx.week, {})
    return {
        "player": {"id": pid, "name": p["name"], "pos": pos, "team": p["team"], "age": p.get("age"),
                   "years_exp": p.get("years_exp"),
                   "depth_chart": f"{p['depth_chart_position']}{p['depth_chart_order']}" if p.get("depth_chart_order") else None,
                   "fantasy_owner": ctx.owner_name(pid), "bye_week": ctx.bye_week(pid)},
        "injury": {
            "status": p.get("injury_status") or "Healthy", "body_part": p.get("injury_body_part"),
            "notes": p.get("injury_notes"), "practice": p.get("practice"),
            "official_report": inj if inj and inj.get("week", 0) >= ctx.week - 1 else None,
        },
        "season": {"games": len(log), "ppg": season_ppg, "total": round(sum(x["pts"] for x in log), 1),
                   "position_rank_ros": ctx.pos_rank.get(pid)},
        "form": {"last3_ppg": last3_ppg, "trend": trend, "last3_games": last3},
        "usage": {"season": usage(log), "last3": usage(last3)},
        "splits": {"home_ppg": _avg(home), "home_games": len(home), "away_ppg": _avg(away), "away_games": len(away)},
        "next_game": next_game,
        "projection": {
            "this_week": detail.get("pts"), "sleeper": detail.get("sleeper"), "huddle_model": detail.get("model"),
            "availability": detail.get("availability"), "note": detail.get("note"),
            "ros_ppg": ctx.ros_ppg(pid), "ros_total_weighted": round(ctx.ros_total.get(pid, 0), 1),
            "vorp": round(ctx.vorp.get(pid, 0), 1), "trade_value": ctx.value.get(pid, 0),
        },
        "upcoming": upcoming,
        "trending_adds_48h": ctx.trending.get(pid),
    }


def compare(ctx: LeagueContext, pids: List[str]) -> List[Dict[str, Any]]:
    out = []
    for pid in pids:
        r = player_report(ctx, pid)
        out.append({
            "player": r["player"], "injury": r["injury"]["status"], "projection": r["projection"],
            "season_ppg": r["season"]["ppg"], "last3_ppg": r["form"]["last3_ppg"], "trend": r["form"]["trend"],
            "usage_last3": r["usage"]["last3"], "next_game": r["next_game"], "splits": r["splits"],
        })
    return out


# ------------------------------------------------------------------ lineups
def lineup_advice(ctx: LeagueContext, roster_id: Optional[int] = None, week: Optional[int] = None) -> Dict[str, Any]:
    rid = roster_id or ctx.my_roster_id
    week = week or ctx.week
    team = ctx.team(rid)
    roster = ctx.active_roster(rid)
    pos = ctx.positions_map()
    lu = Lineup(ctx.slots, roster, pos)
    pts = np.array([ctx.week_proj(p, week) for p in lu.pids]) if lu.pids else np.zeros(0)
    total, assigned = lu.solve(pts)
    starters = [{"slot": s, **(ctx.brief(p, week) if p else {"name": "(empty)", "proj": 0})} for s, p, _ in assigned]
    chosen = {p for _, p, _ in assigned if p}
    bench = sorted((ctx.brief(p, week) for p in roster if p not in chosen), key=lambda b: -b["proj"])
    # compare with what's set in Sleeper right now
    current = [p for p in team["starters"] if p and p != "0"]
    cur_pts = sum(ctx.week_proj(p, week) for p in current)
    swaps = []
    if week == ctx.week and current:
        sit = sorted((p for p in current if p not in chosen and p in ctx.players), key=lambda p: ctx.week_proj(p, week))
        start = sorted((p for p in chosen if p not in current), key=lambda p: -ctx.week_proj(p, week))
        # pair same-position swaps first, then flex swaps
        for a in list(start):
            same = next((b for b in sit if ctx.players[b]["pos"] == ctx.players[a]["pos"]), None)
            if same:
                swaps.append({"start": ctx.brief(a, week), "sit": ctx.brief(same, week)})
                start.remove(a)
                sit.remove(same)
        start = [a for a in start if ctx.week_proj(a, week) > 0.5]   # zero-point starters are handled as holes
        for a, b in zip(start, sit):
            swaps.append({"start": ctx.brief(a, week), "sit": ctx.brief(b, week)})
    # holes: a starting slot with nobody useful (bye, injury, empty) -> stream from waivers
    holes = []
    fa_sorted = sorted(ctx.free_agents, key=lambda p: -ctx.week_proj(p, week))
    for slot, pid, v in assigned:
        if slot not in SLOT_ELIGIBILITY:      # IDP and other slots Huddle doesn't model
            continue
        if pid is not None and ctx.proj_detail.get(pid, {}).get(week, {}).get("note") == "already played":
            continue
        if pid is None or v <= 0.5:
            elig = SLOT_ELIGIBILITY.get(slot, set())
            options = [ctx.brief(p, week) for p in fa_sorted if set(ctx.players[p]["positions"]) & elig][:3]
            holes.append({"slot": slot, "current": ctx.players[pid]["name"] if pid else None,
                          "reason": (ctx.proj_detail.get(pid, {}).get(week, {}).get("note") if pid else "empty slot"),
                          "free_agents": options})
    return {"week": week, "team": team["name"], "projected_total": round(total, 1),
            "current_lineup_total": round(cur_pts, 1) if current else None,
            "starters": starters, "bench": bench, "swaps": swaps, "holes": holes}


def win_probability(mine: float, theirs: float, n_mine: int, n_theirs: int) -> float:
    sd = math.sqrt(sum([(0.5 * mine / max(n_mine, 1)) ** 2] * max(n_mine, 1)) +
                   sum([(0.5 * theirs / max(n_theirs, 1)) ** 2] * max(n_theirs, 1))) or 1.0
    z = (mine - theirs) / sd
    return round(0.5 * (1 + math.erf(z / math.sqrt(2))), 3)


def weekly_matchup(ctx: LeagueContext, week: Optional[int] = None) -> Dict[str, Any]:
    week = week or ctx.week
    try:
        rows = ctx.sleeper.matchups(ctx.league_id, week)
    except Exception:
        rows = []
    mine = next((r for r in rows if int(r.get("roster_id", -1)) == ctx.my_roster_id), None)
    opp_rid = None
    if mine and mine.get("matchup_id") is not None:
        opp = next((r for r in rows if r.get("matchup_id") == mine["matchup_id"]
                    and int(r["roster_id"]) != ctx.my_roster_id), None)
        opp_rid = int(opp["roster_id"]) if opp else None
    me = lineup_advice(ctx, ctx.my_roster_id, week)
    out = {"week": week, "you": me, "opponent": None}
    if opp_rid:
        them = lineup_advice(ctx, opp_rid, week)
        out["opponent"] = them
        out["win_probability"] = win_probability(me["projected_total"], them["projected_total"],
                                                 len(me["starters"]), len(them["starters"]))
        out["actual"] = {"you": mine.get("points"),
                         "opponent": next((r.get("points") for r in rows if int(r["roster_id"]) == opp_rid), None)}
    return out


# ------------------------------------------------------------------ waivers
def waiver_targets(ctx: LeagueContext, position: Optional[str] = None, limit: int = 15) -> Dict[str, Any]:
    from .waivers import waiver_report   # imported here to avoid a circular import
    return waiver_report(ctx, position=position, limit=limit)


# ------------------------------------------------------------------ league
def standings(ctx: LeagueContext) -> List[Dict[str, Any]]:
    pos_map = ctx.positions_map()
    rows = []
    for rid, t in ctx.teams.items():
        tot, _ = season_totals(ctx.slots, ctx.active_roster(rid), pos_map, ctx.proj, ctx.weights)
        rows.append({"roster_id": rid, "team": t["name"], "manager": t["manager"], "wins": t["wins"],
                     "losses": t["losses"], "ties": t["ties"], "points_for": round(t["fpts"], 1),
                     "points_against": round(t["fpts_against"], 1),
                     "ros_lineup_ppg": round(tot / max(float(ctx.weights.sum()), 1), 1),
                     "is_you": rid == ctx.my_roster_id})
    rows.sort(key=lambda r: (-r["wins"], -r["points_for"]))
    ranked = sorted(rows, key=lambda r: -r["ros_lineup_ppg"])
    for i, r in enumerate(ranked, start=1):
        r["power_rank"] = i
    return rows


def dvp_table(ctx: LeagueContext, position: str) -> List[Dict[str, Any]]:
    position = position.upper()
    out = []
    for team, d in ctx.dvp.items():
        m = d.get(position)
        if m:
            out.append({"team": team, "name": TEAM_NAMES.get(team, team), **m})
    out.sort(key=lambda r: r["rank"])
    return out


def roster_view(ctx: LeagueContext, roster_id: Optional[int] = None) -> List[Dict[str, Any]]:
    t = ctx.team(roster_id)
    out = []
    for pid in t["players"]:
        if pid not in ctx.players:
            continue
        b = ctx.brief(pid)
        b["slot_status"] = "IR" if pid in t["reserve"] else ("Taxi" if pid in t["taxi"] else "Active")
        out.append(b)
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}
    out.sort(key=lambda b: (order.get(b["pos"], 9), -b["ros_ppg"]))
    return out
