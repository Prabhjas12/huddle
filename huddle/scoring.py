"""Score any stat line with a Sleeper league's exact scoring settings.

Stats are expressed in Sleeper's stat keys (pass_yd, rec, rush_td, ...). nflverse
rows are converted into the same keys, so actual results, defensive matchup
numbers and projections all use *your* league's scoring.
"""
from __future__ import annotations

import math
from typing import Dict, Mapping

# threshold bonuses: key -> (stat expression, threshold)
_THRESHOLD_BONUSES = {
    "bonus_pass_yd_300": ("pass_yd", 300), "bonus_pass_yd_400": ("pass_yd", 400),
    "bonus_rush_yd_100": ("rush_yd", 100), "bonus_rush_yd_200": ("rush_yd", 200),
    "bonus_rec_yd_100": ("rec_yd", 100), "bonus_rec_yd_200": ("rec_yd", 200),
    "bonus_rush_rec_yd_100": ("rush_rec_yd", 100), "bonus_rush_rec_yd_200": ("rush_rec_yd", 200),
    "bonus_pass_cmp_25": ("pass_cmp", 25), "bonus_rush_att_20": ("rush_att", 20),
}
_REC_BONUS_BY_POS = {"bonus_rec_te": "TE", "bonus_rec_rb": "RB", "bonus_rec_wr": "WR", "bonus_rec_qb": "QB"}
_FD_BONUS_BY_POS = {"bonus_fd_qb": "QB", "bonus_fd_rb": "RB", "bonus_fd_wr": "WR", "bonus_fd_te": "TE"}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def score(stats: Mapping[str, float], scoring: Mapping[str, float], pos: str = "",
          projected: bool = False) -> float:
    """Fantasy points for one stat line.

    For projections (means, not outcomes), yardage bonuses are converted to the
    probability of clearing the threshold so a 95-yard projection still earns
    part of a 100-yard bonus.
    """
    if not stats:
        return 0.0
    s = dict(stats)
    s.setdefault("rush_rec_yd", s.get("rush_yd", 0.0) + s.get("rec_yd", 0.0))
    if projected and pos == "DEF":
        _spread_buckets(s, scoring, "pts_allow", _PTS_BUCKETS, 8.0)
        _spread_buckets(s, scoring, "yds_allow", _YDS_BUCKETS, 55.0)
    total = 0.0
    for key, weight in scoring.items():
        if not isinstance(weight, (int, float)) or weight == 0:
            continue
        if key in _REC_BONUS_BY_POS:
            if pos == _REC_BONUS_BY_POS[key]:
                total += weight * s.get("rec", 0.0)
            continue
        if key in _FD_BONUS_BY_POS:
            if pos == _FD_BONUS_BY_POS[key]:
                total += weight * (s.get("rush_fd", 0.0) + s.get("rec_fd", 0.0))
            continue
        if key in _THRESHOLD_BONUSES and key not in stats:
            stat, threshold = _THRESHOLD_BONUSES[key]
            mean = s.get(stat, 0.0)
            if projected:
                sd = max(0.45 * mean, 1e-6)
                total += weight * (_norm_cdf((mean - threshold) / sd) if mean > 0 else 0.0)
            else:
                total += weight * (1.0 if mean >= threshold else 0.0)
            continue
        val = s.get(key)
        if val:
            total += weight * val
    return round(total, 2)


_PTS_BUCKETS = [(0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
                (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
                (35, 999, "pts_allow_35p")]
_YDS_BUCKETS = [(0, 99, "yds_allow_0_100"), (100, 199, "yds_allow_100_199"), (200, 299, "yds_allow_200_299"),
                (300, 349, "yds_allow_300_349"), (350, 399, "yds_allow_350_399"), (400, 449, "yds_allow_400_449"),
                (450, 499, "yds_allow_450_499"), (500, 549, "yds_allow_500_549"), (550, 9999, "yds_allow_550p")]


def _spread_buckets(s: Dict[str, float], scoring: Mapping[str, float], key: str, buckets, sd: float) -> None:
    """A projection gives expected points/yards allowed; turn that into bucket probabilities."""
    if key not in s or any(b[2] in s for b in buckets):
        return
    if not any(b[2] in scoring for b in buckets):
        return
    mean = s[key]
    for lo, hi, name in buckets:
        p = _norm_cdf((hi + 0.5 - mean) / sd) - _norm_cdf((lo - 0.5 - mean) / sd)
        s[name] = max(p, 0.0)


def describe(scoring: Mapping[str, float]) -> str:
    """One-line human summary of the scoring format."""
    rec = scoring.get("rec", 0)
    fmt = "full PPR" if rec >= 1 else ("half PPR" if rec >= 0.5 else ("standard" if rec == 0 else f"{rec} PPR"))
    parts = [fmt]
    if scoring.get("bonus_rec_te"):
        parts.append(f"TE premium +{scoring['bonus_rec_te']}/rec")
    ptd = scoring.get("pass_td", 4)
    parts.append(f"{ptd:g}-pt passing TDs")
    if scoring.get("pass_int", -1) != -1:
        parts.append(f"INT {scoring.get('pass_int'):g}")
    if scoring.get("rush_fd") or scoring.get("rec_fd"):
        parts.append("first-down points")
    return ", ".join(parts)


# -- nflverse -> Sleeper stat keys ------------------------------------------

def _f(row: Mapping[str, str], key: str) -> float:
    v = row.get(key)
    if v in (None, "", "NA"):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def nflverse_to_stats(row: Mapping[str, str]) -> Dict[str, float]:
    f = lambda k: _f(row, k)  # noqa: E731
    cmp_, att = f("completions"), f("attempts")
    fum_lost = f("fumbles_lost_total") or (
        f("rushing_fumbles_lost") + f("receiving_fumbles_lost") + f("sack_fumbles_lost"))
    s = {
        "pass_yd": f("passing_yards"), "pass_td": f("passing_tds"), "pass_int": f("passing_interceptions"),
        "pass_2pt": f("passing_2pt_conversions"), "pass_cmp": cmp_, "pass_att": att,
        "pass_inc": max(att - cmp_, 0.0), "pass_sack": f("sacks_suffered"), "pass_fd": f("passing_first_downs"),
        "rush_yd": f("rushing_yards"), "rush_td": f("rushing_tds"), "rush_att": f("carries"),
        "rush_2pt": f("rushing_2pt_conversions"), "rush_fd": f("rushing_first_downs"),
        "rec": f("receptions"), "rec_yd": f("receiving_yards"), "rec_td": f("receiving_tds"),
        "rec_tgt": f("targets"), "rec_2pt": f("receiving_2pt_conversions"), "rec_fd": f("receiving_first_downs"),
        "fum_lost": fum_lost, "fum": f("fumbles_total"), "st_td": f("special_teams_tds"),
        "fum_rec_td": f("fumble_recovery_tds"),
        # kicking
        "fgm": f("fg_made"), "fga": f("fg_att"), "fgmiss": f("fg_missed"),
        "fgm_0_19": f("fg_made_0_19"), "fgm_20_29": f("fg_made_20_29"), "fgm_30_39": f("fg_made_30_39"),
        "fgm_40_49": f("fg_made_40_49"), "fgm_50_59": f("fg_made_50_59"), "fgm_60p": f("fg_made_60_"),
        "fgm_50p": f("fg_made_50_59") + f("fg_made_60_"),
        "fgmiss_0_19": f("fg_missed_0_19"), "fgmiss_20_29": f("fg_missed_20_29"),
        "fgmiss_30_39": f("fg_missed_30_39"), "fgmiss_40_49": f("fg_missed_40_49"),
        "fgmiss_50p": f("fg_missed_50_59") + f("fg_missed_60_"),
        "fgm_yds": f("fg_made_distance"),
        "xpm": f("pat_made"), "xpa": f("pat_att"), "xpmiss": f("pat_missed"),
    }
    return {k: v for k, v in s.items() if v}


def defense_stats(def_tot: Mapping[str, float], pts_allowed: float, yds_allowed: float) -> Dict[str, float]:
    """Build a Sleeper-style team DEF stat line."""
    s = dict(def_tot)
    s["pts_allow"] = pts_allowed
    s["yds_allow"] = yds_allowed
    buckets = [(0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
               (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
               (35, 999, "pts_allow_35p")]
    for lo, hi, key in buckets:
        if lo <= pts_allowed <= hi:
            s[key] = 1.0
    ybuckets = [(0, 99, "yds_allow_0_100"), (100, 199, "yds_allow_100_199"), (200, 299, "yds_allow_200_299"),
                (300, 349, "yds_allow_300_349"), (350, 399, "yds_allow_350_399"),
                (400, 449, "yds_allow_400_449"), (450, 499, "yds_allow_450_499"),
                (500, 549, "yds_allow_500_549"), (550, 9999, "yds_allow_550p")]
    for lo, hi, key in ybuckets:
        if lo <= yds_allowed <= hi:
            s[key] = 1.0
    return s
