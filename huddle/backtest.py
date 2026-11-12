"""Backtest: how would Huddle have done this season?

For every week that's already been played, Huddle rebuilds its projections using only
what was known before kickoff:

  * stats from earlier weeks (Huddle's own model: recent form x matchup x Vegas line),
  * that week's matchup ratings, built from earlier weeks only,
  * that week's Vegas lines and official injury report,
  * Sleeper's projections for that week.

Then it compares those projections with what actually happened, two ways:

  1. Accuracy: how many points off each projection was, for every player who mattered
     (startable in a normal league or on a roster in yours), against Sleeper alone,
     Huddle's model alone, and a simple "season average so far" baseline. It also
     tries every Sleeper/Huddle mix to see which would have been most accurate.
  2. Lineups: using your real roster each week (from Sleeper), which lineup Huddle
     would have started, what it would have scored, and whether it would have
     changed any results, next to the lineup you actually played and the best
     possible one in hindsight.
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import spearmanr

from . import scoring as sc
from .lineup import Lineup
from .model import LeagueContext
from .teams import canon

FIRST_WEEK = 3                 # Huddle's model needs a couple of games before it means anything
EVAL_POS = ("QB", "RB", "WR", "TE")
STARTABLE = {"QB": 24, "RB": 48, "WR": 60, "TE": 24}   # roughly who's startable in a normal league
BLENDS = [round(i / 10, 1) for i in range(11)]
PAST_TTL = 7 * 86400           # finished weeks don't change, so cache them for a week
REPORT_AVAILABILITY = {"Out": 0.0, "Doubtful": 0.2, "Questionable": 0.85}
METHODS = ("huddle", "sleeper", "model", "naive")
METHOD_NAMES = {"huddle": "Huddle", "sleeper": "Sleeper alone", "model": "Huddle's model alone",
                "naive": "Season average so far"}


class Backtest:
    def __init__(self, ctx: LeagueContext):
        self.ctx = ctx
        self.blend = float(ctx.settings.get("projection_blend", 0.7))
        self.pos_map = ctx.positions_map()
        self.raw_players: Optional[Dict[str, Dict[str, Any]]] = None
        # each player's games, in order, and each team's finished games
        self.games: Dict[str, Dict[int, Dict[str, Any]]] = {
            pid: {r["week"]: r for r in rows} for pid, rows in ctx.history.items()}
        self.team_played: Dict[str, set] = defaultdict(set)
        for (team, week), g in ctx.nfl.team_week.items():
            if g["final"]:
                self.team_played[team].add(week)

    # ------------------------------------------------------------------ weeks
    def weeks(self) -> List[int]:
        """Every week from FIRST_WEEK whose games are all final, before the current week."""
        by_week: Dict[int, List[bool]] = defaultdict(list)
        for g in self.ctx.nfl.games:
            by_week[g["week"]].append(bool(g["final"]))
        return [w for w in range(FIRST_WEEK, self.ctx.week) if by_week.get(w) and all(by_week[w])]

    # ------------------------------------------------------------ projections
    def _team_in_week(self, pid: str, week: int) -> Optional[str]:
        """The NFL team a player was on that week (players get traded)."""
        p = self.ctx.players[pid]
        if p["pos"] == "DEF":
            return canon(pid)
        g = self.games.get(pid, {})
        if week in g:
            return g[week]["team"]
        earlier = [w for w in g if w < week]
        return g[max(earlier)]["team"] if earlier else p["team"]

    def _availability(self, pid: str, team: Optional[str], week: int, sleeper_raw: Dict[str, Any],
                      sleeper_pts: Optional[float]) -> Tuple[float, Optional[str]]:
        """What Huddle would have known about his status before kickoff."""
        p = self.ctx.players[pid]
        if p["pos"] == "DEF":
            return 1.0, None
        status = self.ctx.nfl.injury_reports.get((p.get("gsis_id"), week))
        if status in REPORT_AVAILABILITY:
            return REPORT_AVAILABILITY[status], status.lower()
        # Players on IR or inactive aren't always on the injury report. Sleeper's pre-game
        # projection says whether it expected him to play: a projection of zero means "not playing".
        if sleeper_pts is not None:
            return (0.0, "Sleeper expected him not to play") if sleeper_pts < 0.5 else (1.0, None)
        if sleeper_raw and p["pos"] == "K":
            return 1.0, None
        # No Sleeper number for him: if he has played this season but sat out his team's
        # last two games, assume he's still out.
        # A player who was on last week's injury report and is off this week's has been cleared.
        # One who's off the report but still not playing is on IR or inactive.
        g = self.games.get(pid, {})
        team_games = sorted(w for w in self.team_played.get(team or "", ()) if w < week)[-2:]
        if g and len(team_games) == 2 and not any(w in g for w in team_games) and any(w < week for w in g):
            if not self.ctx.nfl.injury_reports.get((p.get("gsis_id"), team_games[-1])):
                return 0.0, "missed his last two games and wasn't on the injury report (likely on IR)"
        return 1.0, None

    def project_week(self, week: int, pids: Sequence[str], sleeper_raw: Dict[str, Dict[str, float]],
                     dvp: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Projections for one past week, built only from what was known before it."""
        ctx = self.ctx
        out: Dict[str, Dict[str, Any]] = {}
        for pid in pids:
            p = ctx.players.get(pid)
            if not p:
                continue
            team = self._team_in_week(pid, week)
            game = ctx.nfl.game(team, week)
            sl = sleeper_raw.get(pid)
            sleeper_pts = sc.score(sl, ctx.scoring, p["pos"], projected=True) if sl else None
            rate = ctx._model_rate(pid, before_week=week)
            model = rate * ctx._model_factor(p, game, dvp) if (rate is not None and game) else None
            prior = [r["pts"] for w, r in self.games.get(pid, {}).items() if w < week]
            naive = sum(prior) / len(prior) if prior else None
            avail, why = self._availability(pid, team, week, sleeper_raw, sleeper_pts) if game else (0.0, "bye week")
            actual = self.games.get(pid, {}).get(week, {}).get("pts")
            out[pid] = {"sleeper": sleeper_pts, "model": model, "naive": naive, "avail": avail, "why": why,
                        "actual": actual, "played": actual is not None, "has_game": bool(game)}
        return out

    @staticmethod
    def blended(row: Dict[str, Any], blend: float) -> float:
        """Huddle's projection for a given Sleeper/model mix (same rule the live app uses)."""
        s, m = row["sleeper"], row["model"]
        if not row["has_game"]:
            return 0.0
        if s is not None and m is not None:
            base = blend * s + (1 - blend) * m
        elif s is not None:
            base = s
        elif m is not None:
            base = m
        else:
            base = 0.0
        return max(base, -5.0) * row["avail"]

    # ----------------------------------------------------------------- inputs
    def _fetch(self, weeks: Sequence[int]) -> Tuple[Dict[int, Dict], Dict[int, List[Dict]]]:
        ctx = self.ctx

        def proj(w: int) -> Dict[str, Dict[str, float]]:
            try:
                return ctx.sleeper.projections(ctx.season, w, ttl=PAST_TTL)
            except Exception:
                return {}

        def mups(w: int) -> List[Dict[str, Any]]:
            try:
                return ctx.sleeper.matchups(ctx.league_id, w, ttl=PAST_TTL)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=6) as ex:
            projections = dict(zip(weeks, ex.map(proj, weeks)))
            matchups = dict(zip(weeks, ex.map(mups, weeks)))
        return projections, matchups

    def _population(self, week: int, rostered: set, sleeper_raw: Dict[str, Dict[str, float]]) -> List[str]:
        """Players worth grading for a week: startable in a normal league by what they'd done so
        far (or by Sleeper's projection, for rookies), plus anyone on a roster in your league."""
        ctx = self.ctx
        by_pos: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        for pid, games in self.games.items():
            p = ctx.players.get(pid)
            if not p or p["pos"] not in EVAL_POS:
                continue
            prior = [r["pts"] for w, r in games.items() if w < week]
            if prior:
                by_pos[p["pos"]].append((sum(prior) / len(prior), pid))
        keep = set()
        for pos, rows in by_pos.items():
            keep.update(pid for _, pid in sorted(rows, reverse=True)[:STARTABLE[pos]])
        if sleeper_raw:
            for pos in EVAL_POS:
                ranked = sorted((pid for pid in sleeper_raw if ctx.players.get(pid, {}).get("pos") == pos),
                                key=lambda pid: -sc.score(sleeper_raw[pid], ctx.scoring, pos, projected=True))
                keep.update(ranked[:STARTABLE[pos]])
        keep.update(pid for pid in rostered if ctx.players.get(pid, {}).get("pos") in EVAL_POS)
        return sorted(keep)

    # -------------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        ctx = self.ctx
        weeks = self.weeks()
        if not weeks:
            return {"weeks": [], "ready": False,
                    "message": f"There's nothing to replay yet. The backtest starts once week {FIRST_WEEK} is over."}
        projections, matchups = self._fetch(weeks)
        sleeper_weeks = [w for w in weeks if projections.get(w)]
        rows: List[Dict[str, Any]] = []            # one per player-week that was graded
        lineup_weeks: List[Dict[str, Any]] = []
        league_eff: Dict[str, List[float]] = {"you": [], "huddle": [], "league": []}
        for w in weeks:
            dvp = ctx.dvp_table(before_week=w)
            mu = matchups.get(w) or []
            rostered = {str(p) for m in mu for p in (m.get("players") or [])} or set(ctx.owner_of)
            pop = self._population(w, rostered, projections.get(w) or {})
            need = set(pop) | {str(p) for m in mu for p in (m.get("players") or [])}
            proj = self.project_week(w, sorted(need), projections.get(w) or {}, dvp)
            for pid in pop:
                r = proj.get(pid)
                if not r or not r["played"]:
                    continue
                rows.append({"week": w, "pid": pid, "pos": ctx.players[pid]["pos"], **r})
            if mu:
                lw = self._lineup_week(w, mu, proj, league_eff)
                if lw:
                    lineup_weeks.append(lw)
        accuracy = self._accuracy(rows, weeks, bool(sleeper_weeks))
        lineups = self._lineup_summary(lineup_weeks, league_eff)
        out = {
            "ready": True, "season": ctx.season, "weeks": weeks, "blend": self.blend,
            "sleeper_weeks": sleeper_weeks, "missing_sleeper_weeks": [w for w in weeks if w not in sleeper_weeks],
            "accuracy": accuracy, "lineups": lineups,
        }
        out["summary"] = self._summary(out)
        out["notes"] = self._notes(out)
        return out

    # --------------------------------------------------------------- accuracy
    def _accuracy(self, rows: List[Dict[str, Any]], weeks: List[int], have_sleeper: bool) -> Dict[str, Any]:
        # grade everyone on the same players: those every method has a number for
        need = ("sleeper", "model", "naive") if have_sleeper else ("model", "naive")
        graded = [r for r in rows if all(r[k] is not None for k in need)]
        if not graded:
            return {"n": 0}
        for r in graded:
            r["huddle"] = self.blended(r, self.blend)
            # the single-source methods get the same injury/bye information Huddle had
            r["p_sleeper"] = (r["sleeper"] * r["avail"]) if r["sleeper"] is not None else None
            r["p_model"] = r["model"] * r["avail"]
            r["p_naive"] = r["naive"] * r["avail"]
        preds = {"huddle": lambda r: r["huddle"], "sleeper": lambda r: r["p_sleeper"],
                 "model": lambda r: r["p_model"], "naive": lambda r: r["p_naive"]}
        methods = [m for m in METHODS if have_sleeper or m != "sleeper"]
        actual = np.array([r["actual"] for r in graded])

        def stats(sub: List[Dict[str, Any]], m: str) -> Dict[str, Any]:
            p = np.array([preds[m](r) for r in sub])
            a = np.array([r["actual"] for r in sub])
            err = p - a
            return {"mae": round(float(np.abs(err).mean()), 2), "bias": round(float(err.mean()), 2),
                    "within5": round(float((np.abs(err) <= 5).mean()) * 100, 1), "n": len(sub)}

        def rank_corr(m: str) -> Optional[float]:
            """How well each method ordered players at a position in a given week: the part
            that decides start/sit calls. Averaged over every week and position."""
            vals, wts = [], []
            groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
            for r in graded:
                groups[(r["week"], r["pos"])].append(r)
            for sub in groups.values():
                if len(sub) < 8:
                    continue
                rho = spearmanr([preds[m](r) for r in sub], [r["actual"] for r in sub]).correlation
                if rho is not None and not math.isnan(rho):
                    vals.append(rho)
                    wts.append(len(sub))
            return round(float(np.average(vals, weights=wts)), 3) if vals else None

        groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in graded:
            groups[(r["week"], r["pos"])].append(r)

        def head_to_head(m: str) -> Optional[float]:
            """Start/sit calls: for every two players at the same position in the same week,
            did the method rank the one who scored more ahead? Ties in either are skipped."""
            right = total = 0
            for sub in groups.values():
                if len(sub) < 2:
                    continue
                p = np.array([preds[m](r) for r in sub])
                a = np.array([r["actual"] for r in sub])
                dp = np.sign(p[:, None] - p[None, :])
                da = np.sign(a[:, None] - a[None, :])
                mask = np.triu((dp != 0) & (da != 0), 1)
                right += int((dp[mask] == da[mask]).sum())
                total += int(mask.sum())
            return round(100 * right / total, 1) if total else None

        overall = {m: {**stats(graded, m), "rank_corr": rank_corr(m), "head_to_head": head_to_head(m),
                       "name": METHOD_NAMES[m]} for m in methods}
        by_pos = {}
        for pos in EVAL_POS:
            sub = [r for r in graded if r["pos"] == pos]
            if sub:
                by_pos[pos] = {"n": len(sub), **{m: stats(sub, m)["mae"] for m in methods},
                               "huddle_bias": stats(sub, "huddle")["bias"]}
        by_week = []
        for w in weeks:
            sub = [r for r in graded if r["week"] == w]
            if sub:
                by_week.append({"week": w, "n": len(sub), **{m: stats(sub, m)["mae"] for m in methods}})
        blend_info = self._best_blend(graded, weeks) if have_sleeper else None
        misses = sorted(graded, key=lambda r: -abs(r["huddle"] - r["actual"]))[:8]
        return {
            "n": len(graded), "players": len({r["pid"] for r in graded}), "methods": overall,
            "by_position": by_pos, "by_week": by_week, "blend": blend_info,
            "biggest_misses": [{"week": r["week"], "name": self.ctx.players[r["pid"]]["name"], "pos": r["pos"],
                                "projected": round(r["huddle"], 1), "actual": round(r["actual"], 1)} for r in misses],
            "avg_actual": round(float(actual.mean()), 1),
        }

    def _best_blend(self, graded: List[Dict[str, Any]], weeks: List[int]) -> Dict[str, Any]:
        """Try every Sleeper/model mix. Only recommend a change that's both meaningfully more
        accurate overall and better in most weeks, so one fluky week can't drive it."""
        curve = []
        for b in BLENDS:
            err = [abs(self.blended(r, b) - r["actual"]) for r in graded]
            curve.append({"blend": b, "mae": round(float(np.mean(err)), 3)})
        best = min(curve, key=lambda c: (c["mae"], abs(c["blend"] - self.blend)))
        cur = min(curve, key=lambda c: abs(c["blend"] - self.blend))
        better_weeks = 0
        n_weeks = 0
        for w in weeks:
            sub = [r for r in graded if r["week"] == w]
            if not sub:
                continue
            n_weeks += 1
            e_best = np.mean([abs(self.blended(r, best["blend"]) - r["actual"]) for r in sub])
            e_cur = np.mean([abs(self.blended(r, self.blend) - r["actual"]) for r in sub])
            better_weeks += int(e_best < e_cur)
        gain = cur["mae"] - best["mae"]
        recommend = (best["blend"] != round(self.blend, 1) and gain >= 0.03 and gain / max(cur["mae"], 1e-9) >= 0.005
                     and better_weeks * 2 > n_weeks)
        return {"curve": curve, "best": best["blend"], "best_mae": best["mae"], "current": self.blend,
                "current_mae": cur["mae"], "gain": round(gain, 3), "better_weeks": better_weeks, "weeks": n_weeks,
                "recommend": recommend}

    # ---------------------------------------------------------------- lineups
    def _pos_for(self, pid: str) -> List[str]:
        if pid in self.pos_map:
            return self.pos_map[pid]
        if self.raw_players is None:
            try:
                self.raw_players = self.ctx.sleeper.players()
            except Exception:
                self.raw_players = {}
        p = self.raw_players.get(pid) or {}
        return list(p.get("fantasy_positions") or ([p["position"]] if p.get("position") else []))

    def _solve(self, pids: List[str], pts: Dict[str, float]) -> List[str]:
        pids = [p for p in pids if self._pos_for(p)]
        lu = Lineup(self.ctx.slots, pids, {p: self._pos_for(p) for p in pids})
        if not lu.pids:
            return []
        _, assigned = lu.solve(np.array([pts.get(p, 0.0) for p in lu.pids]))
        return [p for _, p, _ in assigned if p]

    def _lineup_week(self, week: int, mu: List[Dict[str, Any]], proj: Dict[str, Dict[str, Any]],
                     league_eff: Dict[str, List[float]]) -> Optional[Dict[str, Any]]:
        ctx = self.ctx
        mine = next((m for m in mu if int(m.get("roster_id", -1)) == ctx.my_roster_id), None)
        if not mine or not mine.get("players"):
            return None
        results = {}
        for m in mu:
            players = [str(p) for p in (m.get("players") or [])]
            if not players:
                continue
            actual_pts = {str(k): float(v or 0) for k, v in (m.get("players_points") or {}).items()}
            if not actual_pts:      # older payloads: score from stats
                actual_pts = {p: float(proj.get(p, {}).get("actual") or 0.0) for p in players}
            huddle_pts = {p: self.blended(proj[p], self.blend) for p in players if p in proj}
            sleeper_pts = {p: (proj[p]["sleeper"] or 0.0) * proj[p]["avail"] for p in players if p in proj}
            pick = {"huddle": self._solve(players, huddle_pts), "sleeper": self._solve(players, sleeper_pts),
                    "best": self._solve(players, actual_pts)}
            score = {k: round(sum(actual_pts.get(p, 0.0) for p in v), 2) for k, v in pick.items()}
            actual = float(m.get("points") or sum(actual_pts.get(str(p), 0.0) for p in (m.get("starters") or [])))
            results[int(m["roster_id"])] = {"m": m, "pick": pick, "score": score, "actual": round(actual, 2),
                                            "pts": actual_pts, "proj": huddle_pts}
            if score["best"] > 0:
                league_eff["league"].append(actual / score["best"])
        me = results.get(ctx.my_roster_id)
        if not me:
            return None
        if me["score"]["best"] > 0:
            league_eff["you"].append(me["actual"] / me["score"]["best"])
            league_eff["huddle"].append(me["score"]["huddle"] / me["score"]["best"])
        opp = next((r for rid, r in results.items()
                    if rid != ctx.my_roster_id and r["m"].get("matchup_id") is not None
                    and r["m"].get("matchup_id") == mine.get("matchup_id")), None)
        opp_pts = opp["actual"] if opp else None
        starters = [str(p) for p in (mine.get("starters") or []) if p and str(p) != "0"]

        def result(pts: float) -> Optional[str]:
            if opp_pts is None:
                return None
            return "W" if pts > opp_pts else "L" if pts < opp_pts else "T"

        # the calls where Huddle and you disagreed, with what each choice actually scored
        h_only = [p for p in me["pick"]["huddle"] if p not in starters]
        you_only = [p for p in starters if p not in me["pick"]["huddle"]]
        name = lambda p: ctx.players[p]["name"] if p in ctx.players else (self.raw_players or {}).get(p, {}).get(  # noqa: E731
            "full_name") or p
        calls = [{"name": name(p), "pts": round(me["pts"].get(p, 0.0), 1), "proj": round(me["proj"].get(p, 0.0), 1)}
                 for p in h_only]
        benched = [{"name": name(p), "pts": round(me["pts"].get(p, 0.0), 1), "proj": round(me["proj"].get(p, 0.0), 1)}
                   for p in you_only]
        return {
            "week": week, "you": me["actual"], "huddle": me["score"]["huddle"], "sleeper": me["score"]["sleeper"],
            "best": me["score"]["best"], "opponent": opp_pts,
            "opponent_name": ctx.teams.get(int(opp["m"]["roster_id"]), {}).get("name") if opp else None,
            "result": result(me["actual"]), "result_huddle": result(me["score"]["huddle"]),
            "result_sleeper": result(me["score"]["sleeper"]),
            "huddle_would_start": calls, "you_started_instead": benched,
        }

    def _lineup_summary(self, weeks: List[Dict[str, Any]], eff: Dict[str, List[float]]) -> Dict[str, Any]:
        if not weeks:
            return {"available": False}

        def record(key: str) -> Dict[str, int]:
            res = [w[key] for w in weeks if w[key]]
            return {"W": res.count("W"), "L": res.count("L"), "T": res.count("T")}

        n = len(weeks)
        tot = {k: round(sum(w[k] for w in weeks), 1) for k in ("you", "huddle", "sleeper", "best")}
        flips = [{"week": w["week"], "from": w["result"], "to": w["result_huddle"]}
                 for w in weeks if w["result"] and w["result_huddle"] and w["result"] != w["result_huddle"]]
        pct = lambda xs: round(100 * float(np.mean(xs)), 1) if xs else None  # noqa: E731
        return {
            "available": True, "weeks": weeks, "n": n, "totals": tot,
            "per_week": {k: round(v / n, 1) for k, v in tot.items()},
            "huddle_vs_you": round((tot["huddle"] - tot["you"]) / n, 1),
            "huddle_better_weeks": sum(1 for w in weeks if w["huddle"] > w["you"] + 0.05),
            "you_better_weeks": sum(1 for w in weeks if w["you"] > w["huddle"] + 0.05),
            "record": {"you": record("result"), "huddle": record("result_huddle"), "sleeper": record("result_sleeper")},
            "flips": flips,
            "efficiency": {"you": pct(eff["you"]), "huddle": pct(eff["huddle"]), "league": pct(eff["league"])},
        }

    # ---------------------------------------------------------------- summary
    def _summary(self, out: Dict[str, Any]) -> List[str]:
        """The headline findings in plain English."""
        lines: List[str] = []
        acc, lu = out["accuracy"], out["lineups"]
        ws = out["weeks"]
        span = f"weeks {ws[0]}-{ws[-1]}" if len(ws) > 1 else f"week {ws[0]}"
        if lu.get("available"):
            d = lu["huddle_vs_you"]
            r_you, r_h = lu["record"]["you"], lu["record"]["huddle"]
            if abs(d) < 0.5:
                lines.append(f"Huddle's lineups would have scored about the same as yours ({span}).")
            else:
                lines.append(f"Huddle's lineups would have scored {abs(d):.1f} points a week "
                             f"{'more' if d > 0 else 'less'} than the ones you started ({span}).")
            if lu["flips"]:
                won = sum(1 for f in lu["flips"] if f["to"] == "W")
                lost = sum(1 for f in lu["flips"] if f["to"] == "L")
                lines.append(f"That would have changed {len(lu['flips'])} result{'s' if len(lu['flips']) != 1 else ''}: "
                             f"{r_h['W']}-{r_h['L']} instead of your actual {r_you['W']}-{r_you['L']}"
                             + (f" ({won} more win{'s' if won != 1 else ''}" if won else " (")
                             + (f", {lost} more loss{'es' if lost != 1 else ''})" if lost else ")") + ".")
            else:
                lines.append(f"It wouldn't have changed any of your results ({r_you['W']}-{r_you['L']}).")
        m = acc.get("methods") or {}
        if "huddle" in m:
            h = m["huddle"]
            line = f"Huddle's projections were off by {h['mae']:.1f} points per player on average"
            if "sleeper" in m:
                s = m["sleeper"]
                diff = (s["mae"] - h["mae"]) / s["mae"] * 100 if s["mae"] else 0.0
                if abs(diff) < 1:
                    line += f", the same as Sleeper's ({s['mae']:.1f})"
                else:
                    line += f", {abs(diff):.0f}% {'better' if diff > 0 else 'worse'} than Sleeper alone ({s['mae']:.1f})"
            lines.append(line + ".")
            if h.get("head_to_head") is not None:
                hh = f"Choosing between two players at the same position, Huddle picked the one who scored more {h['head_to_head']:.0f}% of the time"
                if "sleeper" in m and m["sleeper"].get("head_to_head") is not None:
                    hh += f" (Sleeper alone: {m['sleeper']['head_to_head']:.0f}%)"
                lines.append(hh + ".")
        b = acc.get("blend")
        if b and b["recommend"]:
            lines.append(f"Mixing Sleeper and Huddle's model {_mix(b['best'])} instead of {_mix(b['current'])} "
                         f"would have been more accurate in {b['better_weeks']} of {b['weeks']} weeks.")
        elif b:
            lines.append(f"Your current mix ({_mix(b['current'])}) is as good as any other mix Huddle tried.")
        return lines

    def _notes(self, out: Dict[str, Any]) -> List[str]:
        notes = ["Fantasy football is noisy: even the best projections miss by 5-6 points per player in a typical "
                 "week. What matters is being a little less wrong, week after week."]
        if out["missing_sleeper_weeks"]:
            notes.append("Sleeper's projections couldn't be loaded for week(s) "
                         + ", ".join(map(str, out["missing_sleeper_weeks"]))
                         + ", so those weeks compare Huddle's model only.")
        if not out["lineups"].get("available"):
            notes.append("Your weekly lineups couldn't be loaded from Sleeper, so the lineup replay is skipped.")
        notes.append("Injury knowledge comes from the official weekly injury report, which is a little less "
                     "up to date than the live app's Sleeper statuses, so this slightly undersells Huddle.")
        return notes


def _mix(b: float) -> str:
    return f"{round(b * 100)}/{round((1 - b) * 100)} Sleeper/Huddle"


def run(ctx: LeagueContext) -> Dict[str, Any]:
    return Backtest(ctx).run()


_cache: Dict[str, Any] = {"key": None, "result": None}
_cache_lock = threading.Lock()


def cached_run(ctx: LeagueContext, force: bool = False) -> Dict[str, Any]:
    """The backtest only changes when the league data is rebuilt or the mix setting changes,
    so keep the last result (one run at a time)."""
    key = (id(ctx), ctx.built_at, float(ctx.settings.get("projection_blend", 0.7)))
    with _cache_lock:
        if not force and _cache["key"] == key and _cache["result"] is not None:
            return _cache["result"]
        result = run(ctx)
        _cache.update(key=key, result=result)
        return result


def for_ai(r: Dict[str, Any]) -> Dict[str, Any]:
    """A compact version for Ask Huddle."""
    if not r.get("ready"):
        return r
    acc, lu = r["accuracy"], r["lineups"]
    out: Dict[str, Any] = {"weeks_replayed": r["weeks"], "headline": r["summary"], "caveats": r["notes"],
                           "projection_accuracy": {m["name"]: {"points_off_per_player": m["mae"],
                                                               "picked_better_of_two_pct": m["head_to_head"],
                                                               "over_or_under": m["bias"]}
                                                   for m in (acc.get("methods") or {}).values()},
                           "by_position_points_off": acc.get("by_position"),
                           "biggest_misses": acc.get("biggest_misses", [])[:5]}
    b = acc.get("blend")
    if b:
        out["sleeper_huddle_mix"] = {k: b[k] for k in ("current", "best", "current_mae", "best_mae", "recommend",
                                                        "better_weeks", "weeks")}
    if lu.get("available"):
        out["lineups"] = {"per_week": lu["per_week"], "record": lu["record"], "flips": lu["flips"],
                          "efficiency_pct_of_best": lu["efficiency"],
                          "weeks": [{k: w[k] for k in ("week", "opponent_name", "you", "huddle", "best", "opponent",
                                                       "result", "result_huddle")}
                                    | {"huddle_would_start": [c["name"] for c in w["huddle_would_start"]],
                                       "you_started_instead": [c["name"] for c in w["you_started_instead"]]}
                                    for w in lu["weeks"]]}
    return out
