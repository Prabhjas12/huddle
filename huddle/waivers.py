"""Waiver wire: who to add, who to drop (or stash on IR), and why.

Everything is judged on *your* season, not one week:
  - your needs: where your starting lineup ranks in the league, and upcoming bye/injury holes
  - for every free agent: lineup points he adds this week, over the rest of the season, and in
    the fantasy playoffs; how many weeks he'd actually start for you; who he'd replace
  - the cheapest way to make room: an open roster spot, an open IR slot for an injured player,
    or cutting the player whose loss hurts least long-term (never a player who still has real
    trade value or is only out short-term)
  - signals: rising snap share, a teammate's injury opening up work, Sleeper's trending adds,
    the next four matchups
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .lineup import Lineup
from .model import LeagueContext
from .reports import player_report

ASSET_COST = 0.5          # lineup points we charge per point of trade value when you cut someone
PROTECT_RANK = 150        # never call a player droppable while the consensus ranks him this high


class _Roster:
    """Week-by-week lineup math for one roster, with per-week starters."""

    def __init__(self, ctx: LeagueContext):
        self.ctx = ctx
        self.pos = ctx.positions_map()
        self.w = ctx.weights
        self.n = len(ctx.ros_weeks)

    def run(self, pids: Sequence[str]) -> Tuple[float, np.ndarray, List[set]]:
        lu = Lineup(self.ctx.slots, list(pids), self.pos)
        mat = np.array([self.ctx.proj.get(p, np.zeros(self.n)) for p in lu.pids]) if lu.pids else np.zeros((0, self.n))
        per_week = np.zeros(self.n)
        starters: List[set] = []
        for i in range(self.n):
            if not lu.pids:
                starters.append(set())
                continue
            tot, assigned = lu.solve(mat[:, i])
            per_week[i] = tot
            starters.append({p for _, p, _ in assigned if p})
        return float((per_week * self.w).sum()), per_week, starters

    def total(self, pids: Sequence[str]) -> float:
        lu = Lineup(self.ctx.slots, list(pids), self.pos)
        if not lu.pids:
            return 0.0
        mat = np.array([self.ctx.proj.get(p, np.zeros(self.n)) for p in lu.pids])
        return float(sum(lu.total(mat[:, i]) * self.w[i] for i in range(self.n)))


def _injury_text(ctx: LeagueContext, pid: str) -> Optional[str]:
    o = ctx.injury_outlook(ctx.players[pid])
    if not o:
        return None
    what = f"{o['status']}" + (f" ({o['body_part']})" if o.get("body_part") else "")
    if o["season_ending"]:
        return what + ", likely out for the season"
    if o["status"] in ("Questionable", "Doubtful"):
        return what + " this week"
    if o["weeks_out"]:
        return what + (f", back around week {o['return_week']}" if o["weeks_out"] > 1 else ", expected back next week")
    return what


# ----------------------------------------------------------------- drop board
def drop_board(ctx: LeagueContext, roster_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every active player ranked from most to least droppable, with the reason."""
    rid = roster_id or ctx.my_roster_id
    R = _Roster(ctx)
    active = ctx.active_roster(rid)
    everyone = ctx.lineup_roster(rid)
    base_tot, _, base_starts = R.run(everyone)
    n = R.n
    ir_open = ctx.open_ir_slots(rid)
    out = []
    for pid in active:
        p = ctx.players[pid]
        loss = base_tot - R.total([x for x in everyone if x != pid])
        starts = sum(1 for s in base_starts if pid in s)
        o = ctx.injury_outlook(p)
        rank = ctx.consensus.get(pid, {}).get("rank")
        value = ctx.value.get(pid, 0.0)
        season_over = bool(o and o["season_ending"])
        protected = rank is not None and rank <= PROTECT_RANK and not season_over
        injured_short = bool(o and not season_over and (o["weeks_out"] or 0) >= 1)
        score = loss + (0.0 if season_over else ASSET_COST * value) + (40.0 if protected else 0.0)
        reasons = []
        if starts:
            reasons.append(f"Starts for you in {starts} of {n} remaining weeks")
        else:
            reasons.append("Doesn't make your starting lineup in any remaining week")
        reasons.append(f"Cutting him costs {loss:.1f} lineup points the rest of the way" if loss >= 0.5
                       else "Cutting him costs you almost nothing in lineup points")
        if rank:
            reasons.append(f"Consensus #{round(rank)} overall" + (", so he still has trade value" if protected else ""))
        else:
            reasons.append("Not in the consensus top 400, so little trade value")
        inj = _injury_text(ctx, pid)
        if inj:
            reasons.insert(0, inj)
        if season_over:
            verdict = "Drop" if not protected else "Keep"
        elif ctx.ir_eligible(pid) and ir_open > 0:
            verdict = "Move to IR"
            reasons.insert(1, "Your league has an open IR slot, so you can keep him and free the roster spot")
        elif injured_short and (protected or starts >= 2):
            verdict = "Keep"
            reasons.insert(1, "Short-term injury; he still helps you after he's back")
        elif protected:
            verdict = "Keep"
        elif loss >= 1.0 * n:           # a real contributor: costs you a point a week or more
            verdict = "Keep"
            reasons.insert(0, "A regular starter for you")
        else:
            verdict = "Droppable"
        out.append({**ctx.brief(pid), "season_over": season_over, "drop_score": round(score, 1), "lineup_loss": round(loss, 1), "starts": starts,
                    "weeks": n, "verdict": verdict, "reasons": reasons})
    out.sort(key=lambda r: (r["verdict"] in ("Keep", "Move to IR"), r["drop_score"]))
    return out


# ----------------------------------------------------------------- needs
def team_needs(ctx: LeagueContext) -> Dict[str, Any]:
    """Where your lineup ranks by position, and holes in the next few weeks."""
    ros = {pid: ctx.ros_ppg(pid) for pid in ctx.players}
    by_team: Dict[int, Dict[str, float]] = {}
    pos_map = ctx.positions_map()
    for rid in ctx.teams:
        roster = ctx.lineup_roster(rid)
        lu = Lineup(ctx.slots, roster, pos_map)
        if not lu.pids:
            continue
        _, assigned = lu.solve(np.array([ros.get(p, 0.0) for p in lu.pids]))
        groups: Dict[str, float] = defaultdict(float)
        for _, pid, v in assigned:
            if pid:
                groups[ctx.players[pid]["pos"]] += v
        by_team[rid] = groups
    me = ctx.my_roster_id
    rows = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        if not any(pos in g for g in by_team.values()):
            continue
        vals = sorted(((g.get(pos, 0.0), rid) for rid, g in by_team.items()), reverse=True)
        rank = next(i for i, (_, rid) in enumerate(vals, start=1) if rid == me)
        avg = sum(v for v, _ in vals) / len(vals)
        rows.append({"pos": pos, "rank": rank, "of": len(vals), "you": round(by_team[me].get(pos, 0.0), 1),
                     "avg": round(avg, 1)})
    weak = [r for r in rows if r["rank"] > max(2, round(0.6 * r["of"])) and r["pos"] not in ("K", "DEF")]
    weak.sort(key=lambda r: (r["you"] - r["avg"]))
    # holes in the next four weeks
    holes = []
    R = _Roster(ctx)
    lu = Lineup(ctx.slots, ctx.lineup_roster(), pos_map)
    for i, w in enumerate(ctx.ros_weeks[:4]):
        if not lu.pids:
            break
        _, assigned = lu.solve(np.array([ctx.proj[p][i] for p in lu.pids]))
        for slot, pid, v in assigned:
            if pid is None or v <= 0.5:
                note = ctx.proj_detail.get(pid, {}).get(w, {}).get("note") if pid else "nobody eligible"
                if note == "already played":
                    continue
                holes.append({"week": w, "slot": slot, "player": ctx.players[pid]["name"] if pid else None,
                              "reason": note or "no projection"})
    del R
    return {"positions": rows, "weakest": weak[:2], "holes": holes}


# ----------------------------------------------------------------- the report
def waiver_report(ctx: LeagueContext, position: Optional[str] = None, limit: int = 15) -> Dict[str, Any]:
    R = _Roster(ctx)
    n = R.n
    me = ctx.my_roster_id
    active = ctx.active_roster(me)
    reserve = [p for p in ctx.lineup_roster(me) if p not in active]
    base_tot, base_week, base_starts = R.run(active + reserve)
    playoff_idx = [i for i, w in enumerate(ctx.ros_weeks) if w >= ctx.playoff_week_start]
    position = position.upper() if position else None
    board = drop_board(ctx, me)
    droppable = [b for b in board if b["verdict"] in ("Drop", "Droppable")][:4]
    ir_moves = [b for b in board if b["verdict"] == "Move to IR"]
    open_spot = len(active) < ctx.roster_limit

    # candidate pool: best projected free agents plus anyone who just inherited work from an injured teammate
    pool = [p for p in ctx.free_agents if position is None or ctx.players[p]["pos"] == position]
    pool.sort(key=lambda p: -(ctx.ros_total.get(p, 0.0) + 3 * ctx.week_proj(p)))
    cands = pool[:45]
    opp = _opportunities(ctx, pool)
    cands += [p for p in opp if p not in cands][:10]

    rows = []
    for fa in cands:
        options: List[Tuple[str, Optional[Dict[str, Any]], List[str]]] = []
        if open_spot:
            options.append(("open", None, active + reserve + [fa]))
        for b in ir_moves[:1]:
            options.append(("ir", b, active + reserve + [fa]))        # he stays yours on IR
        drops = list(droppable)
        same = [b for b in board if b["pos"] == ctx.players[fa]["pos"] and b["verdict"] != "Move to IR"
                and not (ctx.consensus.get(b["id"], {}).get("rank") or 999) <= PROTECT_RANK]
        if same and same[0] not in drops:
            drops.append(same[0])            # swapping like for like (e.g. streaming a kicker)
        for b in drops:
            options.append(("drop", b, [x for x in active + reserve if x != b["id"]] + [fa]))
        if not options:
            continue
        best = None
        for kind, b, roster in options:
            gain = R.total(roster) - base_tot
            cost = ASSET_COST * (b["value"] if kind == "drop" and b and not b["season_over"] else 0.0)
            net = gain - cost
            if best is None or net > best[0]:
                best = (net, gain, kind, b, roster)
        net, gain, kind, b, roster = best
        tot, per_week, starts = R.run(roster)
        delta = per_week - base_week
        n_starts = sum(1 for s in starts if fa in s)
        this_week = float(delta[0])
        playoffs = float(sum(delta[i] for i in playoff_idx))
        replaced = [ctx.players[x]["name"] for x in (base_starts[0] - starts[0]) if b is None or x != b["id"]]
        rep = player_report(ctx, fa)
        snap_l3, snap_s = rep["usage"]["last3"].get("snap_pct"), rep["usage"]["season"].get("snap_pct")
        snap_trend = (snap_l3 - snap_s) if (snap_l3 is not None and snap_s is not None) else None
        adds = ctx.trending.get(fa)
        upcoming = rep["upcoming"]
        soft = sum(1 for u in upcoming if u.get("matchup") in ("Great", "Good"))
        reasons = _reasons(ctx, fa, n_starts, n, this_week, playoffs, replaced, snap_l3, snap_s, snap_trend,
                           opp.get(fa), adds, soft, len(upcoming), per_week, base_week)
        score = (net + 1.0 * max(this_week, 0) + 0.5 * max(playoffs, 0) + (2.0 if (snap_trend or 0) >= 10 else 0)
                 + (3.0 if fa in opp else 0) + min((adds or 0) / 2000.0, 1.5))
        move = ({"kind": "open", "text": "You have an open roster spot"} if kind == "open"
                else {"kind": "ir", "player": b["name"], "id": b["id"], "text": f"Move {b['name']} to IR"} if kind == "ir"
                else {"kind": "drop", "player": b["name"], "id": b["id"], "text": f"Drop {b['name']}",
                      "why": b["reasons"][0] if b else ""})
        row = {**ctx.brief(fa), "ros_gain": round(gain, 1), "net_gain": round(net, 1), "this_week_gain": round(this_week, 1),
               "playoff_gain": round(playoffs, 1), "starts": n_starts, "weeks": n, "move": move, "score": round(score, 2),
               "snap_last3": snap_l3, "snap_season": snap_s, "snap_trend": None if snap_trend is None else round(snap_trend, 1),
               "trending_adds": adds, "opportunity": opp.get(fa), "soft_matchups": soft, "reasons": reasons,
               "replaces_now": replaced[:1]}
        row["bid"] = _bid(ctx, row)
        rows.append(row)

    def top(key, cond=lambda r: True):
        return sorted([r for r in rows if cond(r)], key=key)[:limit]

    views = {
        "best": top(lambda r: -r["score"], lambda r: r["net_gain"] > 0.5 or r["opportunity"]),
        "week": top(lambda r: -r["this_week_gain"], lambda r: r["this_week_gain"] > 0.5),
        "stash": top(lambda r: -((3 if r["opportunity"] else 0) + max(r["snap_trend"] or 0, 0) / 5 + (r["trending_adds"] or 0) / 1500),
                     lambda r: r["opportunity"] or (r["snap_trend"] or 0) >= 8 or (r["trending_adds"] or 0) >= 1000),
        "playoffs": top(lambda r: -r["playoff_gain"], lambda r: r["playoff_gain"] > 0.5),
    }
    faab = None
    if ctx.waiver_type == 2 and ctx.waiver_budget:
        left = ctx.waiver_budget - int(ctx.team()["faab_used"] or 0)
        faab = {"budget": ctx.waiver_budget, "left": left}
    return {
        "needs": team_needs(ctx),
        "recommendations": views["best"][:3],
        "views": views,
        "drop_board": board[:6],
        "faab": faab,
        "roster_status": {"open_spot": open_spot, "ir_open": ctx.open_ir_slots(me)},
        "weeks": ctx.ros_weeks, "playoff_weeks": [ctx.ros_weeks[i] for i in playoff_idx],
    }


def _opportunities(ctx: LeagueContext, pool: List[str]) -> Dict[str, str]:
    """Free agents whose teammate at the same position is hurt, opening up snaps."""
    out: Dict[str, str] = {}
    by_team: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for pid, p in ctx.players.items():
        if p["team"]:
            by_team[(p["team"], p["pos"])].append(pid)
    pool_set = set(pool)
    for (team, pos), ids in by_team.items():
        if pos in ("K", "DEF"):
            continue
        hurt = [x for x in ids if (ctx.injury_outlook(ctx.players[x]) or {}).get("weeks_out")
                and ctx.season_ppg(x) and ctx.season_ppg(x) >= 8]
        if not hurt:
            continue
        star = max(hurt, key=lambda x: ctx.season_ppg(x) or 0)
        o = ctx.injury_outlook(ctx.players[star])
        for x in ids:
            if x in pool_set and x != star:
                out[x] = (f"{ctx.players[star]['name']} is {o['status']}"
                          + ("" if o["season_ending"] else f" (back around week {o['return_week']})" if (o["weeks_out"] or 0) > 1 else "")
                          + f", so more {pos} work should go his way")
    return out


def _reasons(ctx, fa, n_starts, n, this_week, playoffs, replaced, snap_l3, snap_s, snap_trend, opportunity, adds,
             soft, n_up, per_week, base_week) -> List[str]:
    p = ctx.players[fa]
    out = []
    if opportunity:
        out.append(opportunity)
    if n_starts:
        out.append(f"Starts for you in {n_starts} of {n} remaining weeks")
    if this_week > 0.5 and replaced:
        out.append(f"Starts this week over {replaced[0]} ({this_week:+.1f} pts)")
    elif this_week > 0.5:
        out.append(f"Fills a hole in your lineup this week ({this_week:+.1f} pts)")
    # bye/injury coverage weeks
    covers = [w for w, a, b in zip(ctx.ros_weeks, per_week, base_week) if a - b >= 4]
    if covers and n_starts <= max(3, n // 2):
        out.append("Covers your lineup in week " + ", ".join(map(str, covers[:3])))
    if playoffs > 1:
        out.append(f"Adds {playoffs:.1f} lineup points in your fantasy playoffs")
    if snap_trend is not None and snap_trend >= 8:
        out.append(f"Snap share rising: {snap_s:.0f}% this season, {snap_l3:.0f}% over the last 3 games")
    if adds and adds >= 500:
        out.append(f"{adds:,} Sleeper managers added him in the last 48 hours")
    if n_up and soft >= max(2, n_up // 2 + 1):
        out.append(f"Soft schedule: {soft} good or great matchups in the next {n_up} weeks")
    inj = _injury_text(ctx, fa)
    if inj:
        out.append("Injury: " + inj)
    if not out:
        out.append(f"Projects {ctx.ros_ppg(fa):.1f} pts a game the rest of the way")
    return out[:5]


def _bid(ctx: LeagueContext, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not (ctx.waiver_type == 2 and ctx.waiver_budget):
        return None
    left = ctx.waiver_budget - int(ctx.team()["faab_used"] or 0)
    pw = row["net_gain"] / max(row["weeks"], 1)
    lo, hi = (0.20, 0.35) if pw >= 3 else (0.10, 0.20) if pw >= 1.5 else (0.03, 0.10) if pw >= 0.5 else (0.0, 0.03)
    if (row["trending_adds"] or 0) >= 2000 or row["opportunity"]:
        lo, hi = lo * 1.4, hi * 1.4
    return {"low": max(int(round(left * lo)), 0 if hi == 0.03 else 1), "high": max(int(round(left * hi)), 1), "left": left}
