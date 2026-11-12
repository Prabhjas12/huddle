"""Loads free, open NFL data published by the nflverse project (github.com/nflverse).

- weekly player stats (incl. target share, air-yards share, WOPR)
- snap counts
- official injury / practice reports
- schedules with Vegas lines, roof, surface, stadium and weather
- DynastyProcess player-ID crosswalk (Sleeper id <-> GSIS id <-> PFR id)
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .cache import HttpCache
from .scoring import defense_stats, nflverse_to_stats
from .teams import canon

REL = "https://github.com/nflverse/nflverse-data/releases/download"
URLS = {
    "stats": REL + "/stats_player/stats_player_week_{season}.csv",
    "snaps": REL + "/snap_counts/snap_counts_{season}.csv",
    "injuries": REL + "/injuries/injuries_{season}.csv",
    "games": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "ids": "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv",
}
TTL = {"stats": 3 * 3600, "snaps": 6 * 3600, "injuries": 3 * 3600, "games": 6 * 3600, "ids": 86400}
SKILL = ("QB", "RB", "WR", "TE", "K")


def norm_name(name: str) -> str:
    name = (name or "").lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", name)
    return re.sub(r"[^a-z]", "", name)


def _num(v: Any) -> Optional[float]:
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _rows(text: Optional[str]) -> List[Dict[str, str]]:
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


class NFLData:
    def __init__(self, cache: HttpCache, season: int):
        self.cache = cache
        self.season = int(season)
        self.games: List[Dict[str, Any]] = []
        self.game_by_id: Dict[str, Dict[str, Any]] = {}
        self.team_week: Dict[tuple, Dict[str, Any]] = {}       # (team, week) -> game view
        self.byes: Dict[str, List[int]] = {}
        self.weekly: List[Dict[str, Any]] = []                 # skill-player rows
        self.by_player: Dict[str, List[Dict[str, Any]]] = defaultdict(list)   # gsis -> rows
        self.team_totals: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.def_weekly: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(dict)
        self.snaps_by_pfr: Dict[str, Dict[int, float]] = defaultdict(dict)
        self.snaps_by_name: Dict[tuple, Dict[int, float]] = defaultdict(dict)
        self.injuries: Dict[str, Dict[str, Any]] = {}
        self.injury_reports: Dict[tuple, str] = {}             # (gsis, week) -> Out / Doubtful / Questionable
        self.ids_by_sleeper: Dict[str, Dict[str, str]] = {}
        self.fp_to_sleeper: Dict[str, str] = {}
        self.last_completed_week = 0

    def _get(self, key: str, allow_404: bool = False) -> Optional[str]:
        url = URLS[key].format(season=self.season)
        return self.cache.get_text(url, TTL[key], allow_404=allow_404)

    def load(self) -> "NFLData":
        self._load_games()
        self._load_ids()
        self._load_stats()
        self._load_snaps()
        self._load_injuries()
        return self

    # -- schedule ---------------------------------------------------------
    def _load_games(self) -> None:
        for r in _rows(self._get("games")):
            if r.get("season") != str(self.season) or r.get("game_type") != "REG":
                continue
            week = int(r["week"])
            home, away = canon(r["home_team"]), canon(r["away_team"])
            g = {
                "game_id": r["game_id"], "week": week, "gameday": r.get("gameday"),
                "weekday": r.get("weekday"), "gametime": r.get("gametime"),
                "home": home, "away": away, "location": r.get("location"),
                "home_score": _num(r.get("home_score")), "away_score": _num(r.get("away_score")),
                "spread_line": _num(r.get("spread_line")), "total_line": _num(r.get("total_line")),
                "roof": r.get("roof") or None, "surface": r.get("surface") or None,
                "temp": _num(r.get("temp")), "wind": _num(r.get("wind")),
                "stadium": r.get("stadium") or None, "div_game": r.get("div_game") == "1",
            }
            g["final"] = g["home_score"] is not None and g["away_score"] is not None
            self.games.append(g)
            self.game_by_id[g["game_id"]] = g
            for team, opp, is_home in ((home, away, True), (away, home, False)):
                self.team_week[(team, week)] = self._view(g, team, opp, is_home)
            if g["final"]:
                self.last_completed_week = max(self.last_completed_week, week)
        teams = {g["home"] for g in self.games} | {g["away"] for g in self.games}
        max_week = max((g["week"] for g in self.games), default=18)
        for t in teams:
            self.byes[t] = [w for w in range(1, max_week + 1) if (t, w) not in self.team_week]

    @staticmethod
    def _view(g: Dict[str, Any], team: str, opp: str, is_home: bool) -> Dict[str, Any]:
        spread = g["spread_line"]
        total = g["total_line"]
        team_spread = None if spread is None else (spread if is_home else -spread)  # + = favoured
        implied = opp_implied = None
        if total is not None and team_spread is not None:
            implied = round(total / 2 + team_spread / 2, 1)
            opp_implied = round(total / 2 - team_spread / 2, 1)
        neutral = g.get("location") == "Neutral"
        return {
            "game_id": g["game_id"], "week": g["week"], "team": team, "opp": opp,
            "home_away": "neutral" if neutral else ("home" if is_home else "away"),
            "gameday": g["gameday"], "weekday": g["weekday"], "gametime": g["gametime"],
            "total": total, "spread": team_spread, "implied_total": implied, "opp_implied_total": opp_implied,
            "roof": g["roof"], "surface": g["surface"], "temp": g["temp"], "wind": g["wind"],
            "stadium": g["stadium"], "div_game": g["div_game"], "final": g["final"],
            "team_score": g["home_score"] if is_home else g["away_score"],
            "opp_score": g["away_score"] if is_home else g["home_score"],
        }

    def game(self, team: Optional[str], week: int) -> Optional[Dict[str, Any]]:
        return self.team_week.get((canon(team), week)) if team else None

    def is_bye(self, team: Optional[str], week: int) -> bool:
        t = canon(team)
        return bool(t) and week in self.byes.get(t, [])

    # -- ids --------------------------------------------------------------
    def _load_ids(self) -> None:
        try:
            text = self._get("ids")
        except Exception:
            text = None
        for r in _rows(text):
            sid = r.get("sleeper_id")
            if sid and sid != "NA":
                self.ids_by_sleeper[sid] = {"gsis_id": r.get("gsis_id") if r.get("gsis_id") != "NA" else "",
                                            "pfr_id": r.get("pfr_id") if r.get("pfr_id") != "NA" else ""}
                fp = r.get("fantasypros_id")
                if fp and fp != "NA":
                    self.fp_to_sleeper[fp] = sid

    # -- weekly stats -----------------------------------------------------
    def _load_stats(self) -> None:
        def_tot: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        off_yards: Dict[tuple, float] = defaultdict(float)
        for r in _rows(self._get("stats", allow_404=True)):
            if r.get("season_type") != "REG":
                continue
            week = int(r["week"])
            team, opp = canon(r.get("team")), canon(r.get("opponent_team"))
            pos = r.get("position") or ""
            stats = nflverse_to_stats(r)
            # team-level aggregates (all positions)
            tt = self.team_totals[(team, week)]
            tt["rush_att"] += stats.get("rush_att", 0)
            tt["rec_tgt"] += stats.get("rec_tgt", 0)
            tt["pass_att"] += stats.get("pass_att", 0)
            off_yards[(team, week)] += stats.get("pass_yd", 0) + stats.get("rush_yd", 0)
            d = def_tot[(team, week)]
            for src, key in (("def_sacks", "sack"), ("def_interceptions", "int"),
                             ("fumble_recovery_opp", "fum_rec"), ("def_tds", "def_td"),
                             ("def_safeties", "safe"), ("def_fumbles_forced", "ff"),
                             ("def_qb_hits", "qb_hit"), ("def_tackles_for_loss", "tkl_loss")):
                v = _num(r.get(src))
                if v:
                    d[key] += v
            blk = sum(_num(r.get(k)) or 0 for k in ("def_fg_blocks", "def_punt_blocks", "def_pat_blocks"))
            if blk:
                d["blk_kick"] += blk
            if pos not in ("QB", "RB", "WR", "TE", "K"):
                st = _num(r.get("special_teams_tds"))
                if st:
                    d["def_st_td"] += st
                    d["st_td"] += st
                continue
            if stats.get("st_td"):   # returner TDs also count for the team DEF
                d["def_st_td"] += stats["st_td"]
                d["st_td"] += stats["st_td"]
            row = {
                "gsis_id": r["player_id"], "name": r.get("player_display_name") or r.get("player_name"),
                "pos": pos, "team": team, "opp": opp, "week": week, "game_id": r.get("game_id"),
                "stats": stats,
                "target_share": _num(r.get("target_share")), "air_yards_share": _num(r.get("air_yards_share")),
                "wopr": _num(r.get("wopr")), "air_yards": _num(r.get("receiving_air_yards")),
                "headshot": r.get("headshot_url") or None,
            }
            self.weekly.append(row)
            self.by_player[row["gsis_id"]].append(row)
        # team defenses: combine defensive stats with points/yards allowed
        for (team, week), g in self.team_week.items():
            if not g["final"]:
                continue
            self.def_weekly[team][week] = defense_stats(
                def_tot.get((team, week), {}), g["opp_score"] or 0.0, off_yards.get((g["opp"], week), 0.0))
        for rows in self.by_player.values():
            rows.sort(key=lambda x: x["week"])

    # -- snaps ------------------------------------------------------------
    def _load_snaps(self) -> None:
        try:
            text = self._get("snaps", allow_404=True)
        except Exception:
            text = None
        for r in _rows(text):
            if r.get("game_type") not in ("REG", None):
                continue
            pct = _num(r.get("offense_pct"))
            if pct is None:
                continue
            week = int(r["week"])
            if r.get("pfr_player_id"):
                self.snaps_by_pfr[r["pfr_player_id"]][week] = pct
            self.snaps_by_name[(norm_name(r.get("player", "")), canon(r.get("team")))][week] = pct

    # -- injuries ---------------------------------------------------------
    def _load_injuries(self) -> None:
        try:
            text = self._get("injuries", allow_404=True)
        except Exception:
            text = None
        for r in _rows(text):
            if r.get("season_type") not in ("REG", None):
                continue
            gid = r.get("gsis_id")
            week = int(r["week"])
            if r.get("report_status"):
                self.injury_reports[(gid, week)] = r["report_status"]
            cur = self.injuries.get(gid)
            if cur is None or week >= cur["week"]:
                self.injuries[gid] = {
                    "week": week, "report_status": r.get("report_status") or None,
                    "injury": r.get("report_primary_injury") or r.get("practice_primary_injury") or None,
                    "practice_status": r.get("practice_status") or None,
                }
