"""What other managers think players are worth.

Huddle's own projections decide whether a trade helps *your lineup*. But the
other manager judges a trade by perceived value: rankings, names, recent
production. To model that, Huddle uses FantasyPros' expert consensus
rest-of-season PPR rankings (republished weekly by DynastyProcess on GitHub)
and turns the overall rank into a 0-100 value with the steep curve that real
trade charts use, so one top-10 player is worth far more than two top-40s.
"""
from __future__ import annotations

import csv
import io
import math
from typing import Any, Dict, Optional

from .cache import HttpCache
from .teams import canon

URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr_latest.csv"
TTL = 12 * 3600
RANK_SCALE = 40.0     # value halves roughly every 28 spots of overall rank


def rank_to_value(rank: float) -> float:
    return round(100.0 * math.exp(-(max(rank, 1.0) - 1.0) / RANK_SCALE), 1)


def load(cache: HttpCache, fp_to_sleeper: Dict[str, str], def_ids: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """Return {sleeper_id: {"rank": overall ROS rank, "pos_rank": ..., "value": 0-100, "date": ...}}."""
    try:
        text = cache.get_text(URL, TTL)
    except Exception:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    pos_ranks: Dict[str, int] = {}
    rows = [r for r in csv.DictReader(io.StringIO(text or "")) if r.get("page_type") == "redraft-overall"]
    rows.sort(key=lambda r: float(r.get("ecr") or 999))
    for r in rows:
        pos = r.get("pos") or ""
        pos_ranks[pos] = pos_ranks.get(pos, 0) + 1
        if pos == "DST":
            pid: Optional[str] = def_ids.get(canon(r.get("team")) or "")
        else:
            pid = fp_to_sleeper.get(r.get("id") or "")
        if not pid:
            continue
        rank = float(r.get("ecr") or 999)
        out[pid] = {"rank": round(rank, 1), "pos_rank": f"{'DEF' if pos == 'DST' else pos}{pos_ranks[pos]}",
                    "value": rank_to_value(rank), "date": r.get("scrape_date")}
    return out
