"""Optimal-lineup solver for any Sleeper roster configuration.

Uses an assignment solver (Hungarian algorithm) so mixed flex slots such as
FLEX + SUPER_FLEX + REC_FLEX are always filled optimally - a greedy fill can
get these wrong.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

SLOT_ELIGIBILITY = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
NON_STARTING = {"BN", "IR", "TAXI"}
BIG = 1e6


def starting_slots(roster_positions: Sequence[str]) -> List[str]:
    return [s for s in roster_positions if s not in NON_STARTING]


class Lineup:
    """Pre-computes the eligibility matrix for a fixed set of players."""

    def __init__(self, slots: Sequence[str], player_ids: Sequence[str],
                 positions: Dict[str, Iterable[str]]):
        self.slots = list(slots)
        self.pids = list(player_ids)
        elig = np.zeros((len(self.pids), len(self.slots)), dtype=bool)
        for i, pid in enumerate(self.pids):
            pos = set(positions.get(pid) or ())
            for j, slot in enumerate(self.slots):
                elig[i, j] = bool(pos & SLOT_ELIGIBILITY.get(slot, set()))
        self.elig = elig

    def solve(self, points: np.ndarray) -> Tuple[float, List[Tuple[str, Optional[str], float]]]:
        """points: shape (n_players,). Returns (total, [(slot, pid|None, pts)])."""
        if not self.pids or not self.slots:
            return 0.0, [(s, None, 0.0) for s in self.slots]
        cost = np.where(self.elig, -points[:, None], BIG)
        rows, cols = linear_sum_assignment(cost)
        out: List[Tuple[str, Optional[str], float]] = [(s, None, 0.0) for s in self.slots]
        total = 0.0
        for r, c in zip(rows, cols):
            if self.elig[r, c]:
                out[c] = (self.slots[c], self.pids[r], float(points[r]))
                total += float(points[r])
        return total, out

    def total(self, points: np.ndarray) -> float:
        if not self.pids or not self.slots:
            return 0.0
        cost = np.where(self.elig, -points[:, None], BIG)
        rows, cols = linear_sum_assignment(cost)
        mask = self.elig[rows, cols]
        return float(points[rows[mask]].sum())


def season_totals(slots: Sequence[str], player_ids: Sequence[str], positions: Dict[str, Iterable[str]],
                  proj: Dict[str, np.ndarray], weights: np.ndarray) -> Tuple[float, np.ndarray]:
    """Weighted rest-of-season lineup points and the per-week lineup totals."""
    lu = Lineup(slots, player_ids, positions)
    n_weeks = len(weights)
    mat = np.array([proj.get(p, np.zeros(n_weeks)) for p in lu.pids]) if lu.pids else np.zeros((0, n_weeks))
    per_week = np.zeros(n_weeks)
    for w in range(n_weeks):
        per_week[w] = lu.total(mat[:, w]) if len(lu.pids) else 0.0
    return float((per_week * weights).sum()), per_week
