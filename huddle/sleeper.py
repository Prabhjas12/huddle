"""Client for Sleeper's free, public, read-only API (no login needed).

Docs: https://docs.sleeper.com
Projections and weekly stats come from Sleeper's undocumented-but-public
endpoints that power their own app; if those change, Huddle falls back to its
own projection model built from nflverse data.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import HttpCache

API = "https://api.sleeper.app/v1"
API2 = "https://api.sleeper.com"

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


class Sleeper:
    def __init__(self, cache: HttpCache, fixtures_dir: Optional[str] = None):
        self.cache = cache
        self.fixtures = Path(fixtures_dir) if fixtures_dir else (
            Path(os.environ["HUDDLE_FIXTURES"]) if os.environ.get("HUDDLE_FIXTURES") else None
        )

    # -- plumbing ---------------------------------------------------------
    def _get(self, url: str, ttl: float, allow_404: bool = False) -> Any:
        if self.fixtures is not None:
            path = self.fixtures / fixture_name(url)
            if not path.exists():
                return None
            return json.loads(path.read_text())
        return self.cache.get_json(url, ttl, allow_404=allow_404)

    # -- core endpoints ---------------------------------------------------
    def state(self) -> Dict[str, Any]:
        return self._get(f"{API}/state/nfl", ttl=900) or {}

    def user(self, username_or_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"{API}/user/{username_or_id.strip()}", ttl=86400, allow_404=True)

    def leagues(self, user_id: str, season: str) -> List[Dict[str, Any]]:
        return self._get(f"{API}/user/{user_id}/leagues/nfl/{season}", ttl=3600) or []

    def league(self, league_id: str) -> Dict[str, Any]:
        return self._get(f"{API}/league/{league_id}", ttl=3600) or {}

    def rosters(self, league_id: str) -> List[Dict[str, Any]]:
        return self._get(f"{API}/league/{league_id}/rosters", ttl=300) or []

    def users(self, league_id: str) -> List[Dict[str, Any]]:
        return self._get(f"{API}/league/{league_id}/users", ttl=3600) or []

    def matchups(self, league_id: str, week: int, ttl: float = 300) -> List[Dict[str, Any]]:
        return self._get(f"{API}/league/{league_id}/matchups/{week}", ttl=ttl) or []

    def transactions(self, league_id: str, week: int) -> List[Dict[str, Any]]:
        return self._get(f"{API}/league/{league_id}/transactions/{week}", ttl=600) or []

    def players(self) -> Dict[str, Dict[str, Any]]:
        # ~5 MB; Sleeper asks clients to fetch this at most once a day.
        return self._get(f"{API}/players/nfl", ttl=86400) or {}

    def trending(self, kind: str = "add", hours: int = 48, limit: int = 100) -> List[Dict[str, Any]]:
        return self._get(
            f"{API}/players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}", ttl=1800
        ) or []

    # -- projections ------------------------------------------------------
    def projections(self, season: int, week: int, ttl: float = 3 * 3600) -> Dict[str, Dict[str, float]]:
        """Return {player_id: {stat_key: value}} for one week, or {} if unavailable."""
        pos_q = "&".join(f"position[]={p}" for p in FANTASY_POSITIONS)
        candidates = [
            f"{API2}/projections/nfl/{season}/{week}?season_type=regular&{pos_q}",
            f"{API}/projections/nfl/regular/{season}/{week}",
        ]
        for url in candidates:
            try:
                data = self._get(url, ttl=ttl, allow_404=True)
            except Exception:
                data = None
            parsed = parse_stat_payload(data)
            if parsed:
                return parsed
        return {}


def fixture_name(url: str) -> str:
    """File name used for an offline copy of a Sleeper URL (tests / demo mode)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]).strip("_")[:180] + ".json"


def parse_stat_payload(data: Any) -> Dict[str, Dict[str, float]]:
    """Sleeper has returned both a dict keyed by player id and a list of rows."""
    out: Dict[str, Dict[str, float]] = {}
    if not data:
        return out
    if isinstance(data, dict):
        for pid, stats in data.items():
            if isinstance(stats, dict):
                out[str(pid)] = {k: float(v) for k, v in stats.items() if _num(v)}
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            pid = row.get("player_id")
            stats = row.get("stats") or {}
            if pid and isinstance(stats, dict):
                out[str(pid)] = {k: float(v) for k, v in stats.items() if _num(v)}
    return out


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
