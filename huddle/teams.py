"""NFL team code normalisation.

Sleeper, nflverse and DynastyProcess all spell a few team codes differently.
Everything inside Huddle uses the nflverse spelling (LA for the Rams, LV for the
Raiders, ...).
"""
from __future__ import annotations

from typing import Optional

_ALIASES = {
    "LAR": "LA", "RAM": "LA", "STL": "LA",
    "LVR": "LV", "OAK": "LV",
    "SD": "LAC", "SDC": "LAC", "SDG": "LAC",
    "JAC": "JAX",
    "WSH": "WAS",
    "GNB": "GB", "GBP": "GB",
    "KAN": "KC", "KCC": "KC",
    "NOR": "NO", "NOS": "NO",
    "NWE": "NE", "NEP": "NE",
    "SFO": "SF",
    "TAM": "TB", "TBB": "TB",
    "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
}

TEAMS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO",
    "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
]

TEAM_NAMES = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills",
    "CAR": "Panthers", "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns",
    "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers",
    "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "KC": "Chiefs",
    "LA": "Rams", "LAC": "Chargers", "LV": "Raiders", "MIA": "Dolphins",
    "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants",
    "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks",
    "SF": "49ers", "TB": "Buccaneers", "TEN": "Titans", "WAS": "Commanders",
}


def canon(code: Optional[str]) -> Optional[str]:
    """Return the canonical (nflverse) code for a team, or None."""
    if not code:
        return None
    code = str(code).strip().upper()
    if code in ("", "NA", "FA", "FA*", "NONE"):
        return None
    return _ALIASES.get(code, code)
