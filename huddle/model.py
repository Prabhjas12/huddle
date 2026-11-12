"""LeagueContext: everything Huddle knows about your league, rebuilt on refresh.

Pipeline
  1. Sync the league from Sleeper (rosters, scoring, lineup slots, matchups).
  2. Load nflverse stats, snaps, injuries and schedule (with Vegas lines).
  3. Re-score every game this season with *your* league's scoring.
  4. Build defense-vs-position tables from those re-scored games.
  5. Project every remaining week: Sleeper's projections blended with Huddle's
     own model (recent-weighted scoring rate x matchup x Vegas implied total),
     then adjusted for byes and injuries.
  6. Value players against your league's actual waiver wire (VORP).
"""
from __future__ import annotations

import difflib
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import consensus
from . import scoring as sc
from .cache import HttpCache
from .lineup import Lineup, starting_slots
from .nflverse import NFLData, norm_name
from .sleeper import FANTASY_POSITIONS, Sleeper
from .teams import TEAM_NAMES, canon

OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "COV", "NFI"}
STAR_EXPONENT = 1.4   # Huddle value curve: one star is worth more than two half-stars
MARKET_CONSENSUS_WEIGHT = 0.65   # trade value = 65% expert consensus + 35% Huddle's projections


class SetupError(RuntimeError):
    """Raised when the league can't be loaded (missing username, bad league id, ...)."""


def _player_name(p: Dict[str, Any]) -> str:
    if p.get("position") == "DEF":
        team = canon(p.get("team") or p.get("player_id"))
        return f"{TEAM_NAMES.get(team or '', p.get('last_name', ''))} D/ST"
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


class LeagueContext:
    def __init__(self, settings: Dict[str, Any], cache: HttpCache, sleeper: Optional[Sleeper] = None):
        self.settings = settings
        self.cache = cache
        self.sleeper = sleeper or Sleeper(cache)
        self.built_at = 0.0
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ build
    def build(self) -> "LeagueContext":
        t0 = time.time()
        s = self.sleeper
        username = (self.settings.get("sleeper_username") or "").strip()
        league_id = (self.settings.get("league_id") or "").strip()
        if not username:
            raise SetupError("Add your Sleeper username in Settings.")
        user = s.user(username)
        if not user:
            raise SetupError(f"Sleeper has no user called '{username}'. Check the spelling in Settings.")
        self.user_id = str(user["user_id"])
        state = s.state()
        self.season = int(state.get("league_season") or state.get("season") or time.strftime("%Y"))
        season_type = state.get("season_type", "regular")
        week = int(state.get("week") or state.get("display_week") or 1)
        if season_type == "pre" or week < 1:
            week = 1
        if not league_id:
            leagues = s.leagues(self.user_id, str(self.season))
            if not leagues:
                raise SetupError(f"No {self.season} Sleeper leagues found for '{username}'.")
            league_id = leagues[0]["league_id"]
        self.league_id = league_id
        lg = s.league(league_id)
        if not lg:
            raise SetupError(f"Couldn't load Sleeper league {league_id}.")
        self.league = lg
        self.league_name = lg.get("name", "My league")
        self.scoring: Dict[str, float] = {k: float(v) for k, v in (lg.get("scoring_settings") or {}).items()
                                          if isinstance(v, (int, float))}
        self.roster_positions: List[str] = list(lg.get("roster_positions") or [])
        self.slots = starting_slots(self.roster_positions)
        self.roster_limit = sum(1 for p in self.roster_positions if p not in ("IR", "TAXI"))
        ls = lg.get("settings") or {}
        self.reserve_slots = int(ls.get("reserve_slots") or 0)
        self.reserve_rules = {"Out": bool(ls.get("reserve_allow_out")), "Doubtful": bool(ls.get("reserve_allow_doubtful")),
                              "Sus": bool(ls.get("reserve_allow_sus")), "NA": bool(ls.get("reserve_allow_na")),
                              "COV": bool(ls.get("reserve_allow_cov")), "DNR": bool(ls.get("reserve_allow_dnr"))}
        self.waiver_type = int(ls.get("waiver_type") or 0)          # 2 = FAAB
        self.waiver_budget = int(ls.get("waiver_budget") or 0)
        self.num_teams = int(lg.get("total_rosters") or ls.get("num_teams") or 10)
        self.playoff_week_start = int(ls.get("playoff_week_start") or 15)
        playoff_teams = int(ls.get("playoff_teams") or 6)
        rounds = max(1, math.ceil(math.log2(max(playoff_teams, 2))))
        if int(ls.get("playoff_round_type") or 0) == 1:   # two-week championship
            rounds += 1
        self.end_week = min(18, self.playoff_week_start + rounds - 1)
        self.week = max(1, min(week, 18))

        # league members
        users = {str(u["user_id"]): u for u in s.users(league_id)}
        self.teams: Dict[int, Dict[str, Any]] = {}
        self.my_roster_id: Optional[int] = None
        self.owner_of: Dict[str, int] = {}
        for r in s.rosters(league_id):
            rid = int(r["roster_id"])
            owner = str(r.get("owner_id") or "")
            u = users.get(owner, {})
            team_name = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Team {rid}"
            st = r.get("settings") or {}
            players = [str(p) for p in (r.get("players") or [])]
            reserve = [str(p) for p in (r.get("reserve") or [])]
            taxi = [str(p) for p in (r.get("taxi") or [])]
            self.teams[rid] = {
                "roster_id": rid, "owner_id": owner, "name": team_name,
                "manager": u.get("display_name") or team_name,
                "players": players, "reserve": reserve, "taxi": taxi,
                "starters": [str(p) for p in (r.get("starters") or [])],
                "wins": st.get("wins", 0), "losses": st.get("losses", 0), "ties": st.get("ties", 0),
                "faab_used": st.get("waiver_budget_used", 0), "waiver_position": st.get("waiver_position"),
                "fpts": st.get("fpts", 0) + st.get("fpts_decimal", 0) / 100,
                "fpts_against": st.get("fpts_against", 0) + st.get("fpts_against_decimal", 0) / 100,
            }
            for p in players:
                self.owner_of[p] = rid
            co = [str(c) for c in (r.get("co_owners") or [])]
            if owner == self.user_id or self.user_id in co:
                self.my_roster_id = rid
        if self.my_roster_id is None:
            raise SetupError(f"'{username}' doesn't own a team in {self.league_name}. Pick another league in Settings.")

        # players
        raw = s.players()
        self.players: Dict[str, Dict[str, Any]] = {}
        nfl = NFLData(self.cache, self.season).load()
        self.nfl = nfl
        for pid, p in raw.items():
            pos = p.get("position")
            if pos not in FANTASY_POSITIONS:
                continue
            team = canon(p.get("team"))
            if not team and pid not in self.owner_of:
                continue
            if p.get("active") is False and pid not in self.owner_of:
                continue
            fpos = [x for x in (p.get("fantasy_positions") or [pos]) if x in FANTASY_POSITIONS] or [pos]
            gsis = (p.get("gsis_id") or "").strip() or nfl.ids_by_sleeper.get(pid, {}).get("gsis_id", "")
            self.players[pid] = {
                "id": pid, "name": _player_name(p), "pos": pos, "positions": fpos, "team": team,
                "status": p.get("status"), "injury_status": p.get("injury_status"),
                "injury_body_part": p.get("injury_body_part"), "injury_notes": p.get("injury_notes"),
                "injury_start_date": p.get("injury_start_date"),
                "practice": p.get("practice_participation"), "age": p.get("age"),
                "years_exp": p.get("years_exp"), "depth_chart_order": p.get("depth_chart_order"),
                "depth_chart_position": p.get("depth_chart_position"),
                "search_rank": p.get("search_rank") or 999999,
                "gsis_id": gsis, "pfr_id": nfl.ids_by_sleeper.get(pid, {}).get("pfr_id", ""),
                "norm": norm_name(_player_name(p).replace(" D/ST", "")),
                "initials": _initials(_player_name(p)),
            }
        self.gsis_to_pid = {p["gsis_id"]: pid for pid, p in self.players.items() if p["gsis_id"]}

        self.ros_weeks = list(range(self.week, self.end_week + 1)) or [self.week]
        self.weights = np.array([float(self.settings.get("playoff_weight", 1.25))
                                 if w >= self.playoff_week_start else 1.0 for w in self.ros_weeks])
        self._score_history()
        self._build_dvp()
        self._build_projections()
        self._build_values()
        self._load_trending()
        self.built_at = time.time()
        self.build_seconds = round(time.time() - t0, 1)
        return self

    # --------------------------------------------------------------- history
    def _score_history(self) -> None:
        """Fantasy points for every game this season, in this league's scoring."""
        self.history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)   # pid -> game rows
        self.weekly_pts: List[Tuple[str, str, int, float, str]] = []         # (opp, pos, week, pts, pid/gsis)
        for row in self.nfl.weekly:
            pid = self.gsis_to_pid.get(row["gsis_id"])
            pos = self.players[pid]["pos"] if pid else row["pos"]
            pts = sc.score(row["stats"], self.scoring, pos)
            row = dict(row, pts=pts)
            if pid:
                self.history[pid].append(row)
            self.weekly_pts.append((row["opp"], row["pos"], row["week"], pts, row["gsis_id"]))
        self.def_history: Dict[str, List[Dict[str, Any]]] = {}
        for team, weeks in self.nfl.def_weekly.items():
            rows = []
            for w, stats in sorted(weeks.items()):
                g = self.nfl.game(team, w)
                rows.append({"week": w, "opp": g["opp"] if g else None, "stats": stats,
                             "pts": sc.score(stats, self.scoring, "DEF"), "game_id": g["game_id"] if g else None})
            self.def_history[team] = rows
        for pid in list(self.players):
            if self.players[pid]["pos"] == "DEF":
                self.history[pid] = self.def_history.get(canon(pid) or "", [])

    # ------------------------------------------------------------------ DvP
    def _build_dvp(self) -> None:
        self.dvp = self.dvp_table()

    def dvp_table(self, before_week: Optional[int] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Average fantasy points each defense allows per game to each position.
        before_week limits it to games before that week (used by the backtest)."""
        early = (lambda w: True) if before_week is None else (lambda w: w < before_week)
        sums: Dict[Tuple[str, str], float] = defaultdict(float)
        games_played: Dict[str, set] = defaultdict(set)
        for (team, week), g in self.nfl.team_week.items():
            if g["final"] and early(week):
                games_played[team].add(week)
        for opp, pos, week, pts, _ in self.weekly_pts:
            if opp and pos in ("QB", "RB", "WR", "TE", "K") and early(week):
                sums[(opp, pos)] += pts
        # DEF: fantasy points opposing defenses score against each offense
        for team, rows in self.def_history.items():
            for r in rows:
                if r["opp"] and early(r["week"]):
                    sums[(r["opp"], "DEF")] += r["pts"]
        dvp: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            vals = {}
            for team, weeks in games_played.items():
                if weeks:
                    vals[team] = sums.get((team, pos), 0.0) / len(weeks)
            if not vals:
                continue
            avg = sum(vals.values()) / len(vals)
            ranked = sorted(vals, key=lambda t: -vals[t])
            for i, team in enumerate(ranked, start=1):
                mult = vals[team] / avg if avg else 1.0
                dvp[team][pos] = {
                    "allowed_ppg": round(vals[team], 1), "league_avg": round(avg, 1),
                    "rank": i, "mult": round(mult, 3), "label": _matchup_label(i, len(ranked)),
                    "games": len(games_played[team]),
                }
        return dvp

    def matchup(self, opp: Optional[str], pos: str) -> Optional[Dict[str, Any]]:
        if not opp:
            return None
        return self.dvp.get(canon(opp), {}).get(pos)

    # ----------------------------------------------------------- projections
    def _model_rate(self, pid: str, before_week: Optional[int] = None) -> Optional[float]:
        """Huddle's own per-game rate: last 10 games, the last 3 counted double.
        before_week only uses games before that week (used by the backtest)."""
        rows = sorted(self.history.get(pid) or [], key=lambda r: r["week"])
        if before_week is not None:
            rows = [r for r in rows if r["week"] < before_week]
        if not rows:
            return None
        rows = rows[-10:]
        weights = [2.0 if i >= len(rows) - 3 else 1.0 for i in range(len(rows))]
        rate = sum(r["pts"] * w for r, w in zip(rows, weights)) / sum(weights)
        if len(rows) < 3:
            rate *= 0.85   # small sample: regress a little
        return rate

    def _model_factor(self, p: Dict[str, Any], game: Dict[str, Any],
                      dvp: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None) -> float:
        m = (self.dvp if dvp is None else dvp).get(canon(game["opp"]) or "", {}).get(p["pos"])
        mult = 1.0 + 0.5 * ((m["mult"] if m else 1.0) - 1.0)
        mult = min(max(mult, 0.8), 1.2)
        vegas = 1.0
        if p["pos"] == "DEF":
            if game.get("opp_implied_total"):
                vegas = 1.0 + 0.6 * (22.0 / max(game["opp_implied_total"], 10.0) - 1.0)
        elif game.get("implied_total"):
            vegas = 1.0 + 0.5 * (game["implied_total"] / 22.0 - 1.0)
        vegas = min(max(vegas, 0.85), 1.15)
        return math.exp(0.5 * math.log(mult) + 0.5 * math.log(vegas))

    def week_of_date(self, date: Optional[str]) -> Optional[int]:
        """NFL week that a calendar date (YYYY-MM-DD) falls in."""
        if not date:
            return None
        date = str(date)[:10]
        firsts = {}
        for g in self.nfl.games:
            if g.get("gameday"):
                firsts[g["week"]] = min(firsts.get(g["week"], g["gameday"]), g["gameday"])
        weeks = [w for w, d in sorted(firsts.items()) if d <= date]
        return weeks[-1] if weeks else 1

    def injury_outlook(self, p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Best guess at how long an injured player is out.

        IR is a 4-game minimum in the NFL; "Out" is usually week to week; season-ending
        language in the injury notes (ACL, Achilles, "out for the season") means no return.
        """
        status = p.get("injury_status")
        if not status:
            return None
        notes = f"{p.get('injury_notes') or ''} {p.get('injury_body_part') or ''}".lower()
        season_words = ("season-ending", "season ending", "out for the season", "for the season", "rest of the season",
                        "acl", "achilles", "torn")
        season_ending = status in ("IR", "PUP", "NFI", "Out") and any(w in notes for w in season_words)
        if season_ending:
            back = 99
        elif status in ("IR", "PUP", "NFI"):
            placed = self.week_of_date(p.get("injury_start_date"))
            back = max(self.week + 1, (placed or self.week - 1) + 4)
        elif status in ("Out", "Sus", "NA", "DNR", "COV"):
            back = self.week + 1
        else:                      # Questionable / Doubtful: may play this week
            back = self.week
        return {"status": status, "body_part": p.get("injury_body_part"), "return_week": back,
                "weeks_out": max(0, back - self.week) if back < 99 else None, "season_ending": season_ending,
                "practice": p.get("practice")}

    def availability(self, p: Dict[str, Any], week: int) -> Tuple[float, Optional[str]]:
        """Fraction of a normal projection we expect, plus a reason."""
        if not p.get("team"):
            return 0.0, "not on an NFL team"
        if self.nfl.is_bye(p["team"], week):
            return 0.0, "bye week"
        o = self.injury_outlook(p)
        if not o:
            return 1.0, None
        what = f"{o['status']} ({o['body_part'] or 'injury'})"
        if o["season_ending"]:
            return 0.0, what + ", likely out for the season"
        if week == self.week and o["status"] == "Questionable":
            return 0.85, "questionable"
        if week == self.week and o["status"] == "Doubtful":
            return 0.2, "doubtful"
        if week < o["return_week"]:
            return 0.0, what + (f", back around week {o['return_week']}" if o["weeks_out"] and o["weeks_out"] > 1 else "")
        if week == o["return_week"] and (o["weeks_out"] or 0) > 0:
            return 0.8, "first game back"
        return 1.0, None

    def ir_eligible(self, pid: str) -> bool:
        st = self.players.get(pid, {}).get("injury_status")
        return bool(st) and (st in ("IR", "PUP", "NFI") or self.reserve_rules.get(st, False))

    def open_ir_slots(self, roster_id: Optional[int] = None) -> int:
        return max(0, self.reserve_slots - len(self.team(roster_id)["reserve"]))

    def lineup_roster(self, roster_id: Optional[int] = None) -> List[str]:
        """Everyone who can score for you this season, including players on IR (they count once they're back)."""
        t = self.team(roster_id)
        return [p for p in t["players"] if p not in t["taxi"] and p in self.players]

    def _build_projections(self) -> None:
        weeks = self.ros_weeks
        blend = float(self.settings.get("projection_blend", 0.7))
        with ThreadPoolExecutor(max_workers=6) as ex:
            sleeper_proj = dict(zip(weeks, ex.map(lambda w: self._safe_proj(w), weeks)))
        self.sleeper_projection_weeks = [w for w in weeks if sleeper_proj.get(w)]
        if not self.sleeper_projection_weeks:
            self.warnings.append("Sleeper projections were unavailable, so projections use Huddle's own model only.")
        self.proj: Dict[str, np.ndarray] = {}
        self.proj_detail: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for pid, p in self.players.items():
            rate = self._model_rate(pid)
            vec = np.zeros(len(weeks))
            detail: Dict[int, Dict[str, Any]] = {}
            played = {r["week"]: r["pts"] for r in self.history.get(pid, [])}
            for i, w in enumerate(weeks):
                game = self.nfl.game(p["team"], w)
                if w == self.week and game and game["final"]:
                    # game already played this week (e.g. Thursday night): use what actually happened
                    pts = played.get(w, 0.0)
                    vec[i] = pts
                    detail[w] = {"pts": pts, "sleeper": None, "model": None, "availability": 1.0,
                                 "note": "already played"}
                    continue
                avail, reason = self.availability(p, w)
                model = None
                if rate is not None and game:
                    model = rate * self._model_factor(p, game)
                sl = sleeper_proj.get(w, {}).get(pid)
                sleeper_pts = sc.score(sl, self.scoring, p["pos"], projected=True) if sl else None
                if sleeper_pts is not None and model is not None:
                    base = blend * sleeper_pts + (1 - blend) * model
                elif sleeper_pts is not None:
                    base = sleeper_pts
                elif model is not None:
                    base = model
                else:
                    base = 0.0
                if not game:
                    base = 0.0
                pts = max(base, -5.0) * avail
                vec[i] = round(pts, 2)
                detail[w] = {"pts": round(pts, 2), "sleeper": None if sleeper_pts is None else round(sleeper_pts, 2),
                             "model": None if model is None else round(model, 2),
                             "availability": avail, "note": reason}
            self.proj[pid] = vec
            self.proj_detail[pid] = detail

    def _safe_proj(self, week: int) -> Dict[str, Dict[str, float]]:
        try:
            return self.sleeper.projections(self.season, week)
        except Exception:
            return {}

    def week_proj(self, pid: str, week: Optional[int] = None) -> float:
        week = week or self.week
        if week not in self.ros_weeks:
            return 0.0
        return float(self.proj.get(pid, np.zeros(len(self.ros_weeks)))[self.ros_weeks.index(week)])

    # ---------------------------------------------------------------- values
    def _build_values(self) -> None:
        self.ros_total = {pid: float((vec * self.weights).sum()) for pid, vec in self.proj.items()}
        self.free_agents = [pid for pid in self.players if pid not in self.owner_of and self.players[pid]["team"]]
        self.replacement: Dict[str, float] = {}
        for pos in FANTASY_POSITIONS:
            fa = sorted((self.ros_total[p] for p in self.free_agents if self.players[p]["pos"] == pos), reverse=True)
            self.replacement[pos] = fa[min(2, len(fa) - 1)] if fa else 0.0
        self.vorp = {pid: max(0.0, self.ros_total[pid] - self.replacement.get(self.players[pid]["pos"], 0.0))
                     for pid in self.players}
        top = max(self.vorp.values()) if self.vorp else 1.0
        top = top or 1.0
        # Huddle's own view: value over your league's waiver wire, with a star premium
        self.model_value = {pid: round(100 * (v / top) ** STAR_EXPONENT, 1) for pid, v in self.vorp.items()}
        # The market's view: expert consensus rest-of-season rankings
        def_ids = {canon(pid): pid for pid, p in self.players.items() if p["pos"] == "DEF"}
        self.consensus = consensus.load(self.cache, self.nfl.fp_to_sleeper, def_ids)
        if not self.consensus:
            self.warnings.append("Consensus rankings were unavailable, so trade values use Huddle's projections only.")
        cw = MARKET_CONSENSUS_WEIGHT if self.consensus else 0.0
        self.value = {pid: round(cw * self.consensus.get(pid, {}).get("value", 0.0) + (1 - cw) * self.model_value[pid], 1)
                      for pid in self.players}
        # positional ranks by rest-of-season points per game
        self.pos_rank: Dict[str, int] = {}
        for pos in FANTASY_POSITIONS:
            ids = sorted((p for p in self.players if self.players[p]["pos"] == pos),
                         key=lambda p: -self.ros_total[p])
            for i, p in enumerate(ids, start=1):
                self.pos_rank[p] = i

    def _load_trending(self) -> None:
        self.trending: Dict[str, int] = {}
        try:
            for t in self.sleeper.trending("add", 48, 100):
                self.trending[str(t.get("player_id"))] = int(t.get("count", 0))
        except Exception:
            pass

    # --------------------------------------------------------------- helpers
    def team(self, roster_id: Optional[int] = None) -> Dict[str, Any]:
        return self.teams[roster_id or self.my_roster_id]

    def active_roster(self, roster_id: Optional[int] = None) -> List[str]:
        t = self.team(roster_id)
        skip = set(t["reserve"]) | set(t["taxi"])
        return [p for p in t["players"] if p not in skip and p in self.players]

    def positions_map(self) -> Dict[str, List[str]]:
        return {pid: p["positions"] for pid, p in self.players.items()}

    def owner_name(self, pid: str) -> str:
        rid = self.owner_of.get(pid)
        if rid is None:
            return "Free agent"
        return "You" if rid == self.my_roster_id else self.teams[rid]["name"]

    def ros_ppg(self, pid: str) -> float:
        vec = self.proj.get(pid)
        if vec is None:
            return 0.0
        played = [v for w, v in zip(self.ros_weeks, vec) if not self.nfl.is_bye(self.players[pid]["team"], w)]
        return round(float(np.mean(played)), 1) if played else 0.0

    def season_ppg(self, pid: str) -> Optional[float]:
        rows = self.history.get(pid) or []
        return round(sum(r["pts"] for r in rows) / len(rows), 1) if rows else None

    def bye_week(self, pid: str) -> Optional[int]:
        team = self.players.get(pid, {}).get("team")
        byes = [w for w in self.nfl.byes.get(team or "", []) if w <= 18]
        return byes[0] if byes else None

    def brief(self, pid: str, week: Optional[int] = None) -> Dict[str, Any]:
        """Compact player summary used across the UI and AI tools."""
        p = self.players[pid]
        week = week or self.week
        g = self.nfl.game(p["team"], week)
        m = self.matchup(g["opp"], p["pos"]) if g else None
        return {
            "id": pid, "name": p["name"], "pos": p["pos"], "team": p["team"],
            "injury": p.get("injury_status"), "owner": self.owner_name(pid),
            "proj": round(self.week_proj(pid, week), 1), "ros_ppg": self.ros_ppg(pid),
            "ros_total": round(self.ros_total.get(pid, 0.0), 1), "value": self.value.get(pid, 0.0),
            "season_ppg": self.season_ppg(pid), "pos_rank": self.pos_rank.get(pid),
            "consensus_rank": self.consensus.get(pid, {}).get("rank"),
            "consensus_pos_rank": self.consensus.get(pid, {}).get("pos_rank"),
            "bye": self.bye_week(pid),
            "opp": (("vs " if g["home_away"] != "away" else "@ ") + g["opp"]) if g else ("BYE" if self.nfl.is_bye(p["team"], week) else None),
            "matchup": m["label"] if m else None, "matchup_rank": m["rank"] if m else None,
            "implied_total": g.get("implied_total") if g else None,
            "note": self.proj_detail.get(pid, {}).get(week, {}).get("note"),
        }

    # ----------------------------------------------------------- name search
    def find_player(self, query: str, pool: Optional[List[str]] = None) -> Optional[str]:
        res = self.search(query, limit=1, pool=pool)
        return res[0] if res else None

    def search(self, query: str, limit: int = 8, pool: Optional[List[str]] = None) -> List[str]:
        q = (query or "").strip()
        if not q:
            return []
        if q in self.players:
            return [q]
        cands = pool if pool is not None else list(self.players)
        qn = norm_name(q.replace("D/ST", "").replace("DST", ""))
        # team defenses: "Steelers", "PIT", "Steelers D/ST"
        tq = canon(q.split()[0]) if q.split() else None
        def_hits = [pid for pid in cands if self.players[pid]["pos"] == "DEF"
                    and (canon(pid) == tq or norm_name(TEAM_NAMES.get(canon(pid) or "", "")) == qn)]
        if def_hits and ("def" in q.lower() or "d/st" in q.lower() or len(q) <= 3
                         or norm_name(TEAM_NAMES.get(canon(def_hits[0]) or "", "")) == qn):
            return def_hits[:limit]

        def rel(pid: str) -> float:
            p = self.players[pid]
            r = 0.0 if pid in self.owner_of else 1.0
            return r * 1e6 + p["search_rank"]

        exact = [pid for pid in cands if self.players[pid]["norm"] == qn]
        if exact:
            return sorted(exact, key=rel)[:limit]
        sub = [pid for pid in cands if qn and qn in self.players[pid]["norm"]]
        if sub:
            return sorted(sub, key=rel)[:limit]
        # last-name match ("Gibbs")
        last = [pid for pid in cands if norm_name(self.players[pid]["name"].split()[-1]) == qn]
        if last:
            return sorted(last, key=rel)[:limit]
        # initials ("JSN", "CMC", "ARSB")
        if 2 <= len(qn) <= 5 and q.replace(".", "").isalpha() and " " not in q:
            ini = [pid for pid in cands if self.players[pid].get("initials") == qn]
            if ini:
                return sorted(ini, key=rel)[:limit]
        names = {self.players[pid]["norm"]: pid for pid in sorted(cands, key=rel, reverse=True)}
        close = difflib.get_close_matches(qn, list(names), n=limit, cutoff=0.72)
        return [names[c] for c in close]


def _initials(name: str) -> str:
    import re
    parts = [x for x in re.split(r"[\s\-]+", name.replace(".", " ")) if x and x.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    out = ""
    for part in parts:
        # McCaffrey -> "mc" counts as "c" in CMC
        if part.lower().startswith("mc") and len(part) > 2:
            out += "m" + part[2].lower()
        else:
            out += part[0].lower()
    return out


def _matchup_label(rank: int, n: int) -> str:
    """rank 1 = defense allows the most points to that position (best matchup)."""
    frac = rank / max(n, 1)
    if frac <= 0.16:
        return "Great"
    if frac <= 0.38:
        return "Good"
    if frac <= 0.63:
        return "Neutral"
    if frac <= 0.85:
        return "Tough"
    return "Brutal"
