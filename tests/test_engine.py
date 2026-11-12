"""Run with:  .venv/bin/python -m pip install pytest && .venv/bin/python -m pytest tests
The integration tests download this season's nflverse files (a few MB) the first time.
"""
import csv
import io
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from huddle.cache import HttpCache  # noqa: E402
from huddle.lineup import Lineup  # noqa: E402
from huddle.scoring import nflverse_to_stats, score  # noqa: E402

SEASON = 2026


def test_mixed_flex_is_solved_optimally():
    # A greedy fill puts the WR in REC_FLEX and the RB in WRRB_FLEX (12 pts); optimal is 19.
    lu = Lineup(["REC_FLEX", "WRRB_FLEX"], ["wr", "te", "rb"], {"wr": ["WR"], "te": ["TE"], "rb": ["RB"]})
    total, _ = lu.solve(np.array([10.0, 9.0, 2.0]))
    assert total == pytest.approx(19.0)


def test_empty_slot_when_nobody_is_eligible():
    lu = Lineup(["QB", "K"], ["qb"], {"qb": ["QB"]})
    total, assigned = lu.solve(np.array([20.0]))
    assert total == 20.0 and assigned[1][1] is None


def test_te_premium_and_yardage_bonus():
    stats = {"rec": 5, "rec_yd": 104, "rec_td": 1}
    sc = {"rec": 1, "rec_yd": 0.1, "rec_td": 6, "bonus_rec_te": 0.5, "bonus_rec_yd_100": 3}
    assert score(stats, sc, "TE") == pytest.approx(5 + 10.4 + 6 + 2.5 + 3)
    assert score(stats, sc, "WR") == pytest.approx(5 + 10.4 + 6 + 3)


@pytest.fixture(scope="session")
def nflverse_dir(tmp_path_factory):
    from huddle.nflverse import URLS
    d = tmp_path_factory.mktemp("nflverse")
    cache = HttpCache(ROOT / "data" / "cache")
    for key, name in (("stats", "stats_player_week_2026.csv"), ("ids", "db_playerids.csv"),
                      ("injuries", "injuries_2026.csv")):
        (d / name).write_text(cache.get_text(URLS[key].format(season=SEASON), 86400))
    return d


def test_scoring_matches_nflverse(nflverse_dir):
    sc = {"pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
          "rec": 1, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2, "st_td": 6}
    rows = [r for r in csv.DictReader(io.StringIO((nflverse_dir / "stats_player_week_2026.csv").read_text()))
            if r["position"] in ("QB", "RB", "WR", "TE")]
    for r in rows:
        st = nflverse_to_stats(r)
        st["fum_lost"] = sum(float(r[k] or 0) for k in ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"))
        assert score(st, sc, r["position"]) == pytest.approx(float(r["fantasy_points_ppr"]), abs=0.05)


@pytest.fixture(scope="session")
def ctx(nflverse_dir, tmp_path_factory):
    import make_fixtures
    from huddle.model import LeagueContext
    from huddle.sleeper import Sleeper
    fx = tmp_path_factory.mktemp("fixtures")
    make_fixtures.main(nflverse_dir, fx, with_proj=True)
    cache = HttpCache(ROOT / "data" / "cache")
    return LeagueContext({"sleeper_username": "testuser", "projection_blend": 0.7, "playoff_weight": 1.25},
                         cache, Sleeper(cache, fixtures_dir=str(fx))).build()


def test_league_builds(ctx):
    assert ctx.my_roster_id == 1 and len(ctx.teams) == 10
    assert ctx.sleeper_projection_weeks, "fixture projections should be parsed"
    assert all(len(v) == len(ctx.ros_weeks) for v in ctx.proj.values())


def test_bye_weeks_project_zero(ctx):
    for pid, p in ctx.players.items():
        for i, w in enumerate(ctx.ros_weeks):
            if ctx.nfl.is_bye(p["team"], w):
                assert ctx.proj[pid][i] == 0


def test_trade_respects_roster_limits_and_is_zero_sum_in_players(ctx):
    from huddle.trades import TradeEngine
    eng = TradeEngine(ctx)
    mine = sorted(ctx.active_roster(), key=lambda p: -ctx.value[p])
    other = next(r for r in ctx.teams if r != ctx.my_roster_id)
    theirs = sorted(ctx.active_roster(other), key=lambda p: -ctx.value[p])
    r = eng.analyze(mine[1:3], theirs[:1])
    assert r["partner"]["roster_id"] == other
    assert len(r["weeks"]) == len(ctx.ros_weeks)
    # reported cuts are players they own; reported adds are free agents
    them_before = set(ctx.active_roster(other))
    assert all(p["id"] in them_before for p in r["them"]["drops"])
    assert all(p["id"] not in them_before for p in r["them"]["adds"])
    # receiving two players for one forces a cut to stay at the roster limit
    eng._scope(other)
    after, drops, _ = eng.settle(ctx.active_roster(other) + mine[1:3], set(mine[1:3]), 0, set(ctx.owner_of))
    assert len(after) <= ctx.roster_limit and len(drops) >= 1
    # a trade with yourself is rejected
    with pytest.raises(ValueError):
        eng.analyze(mine[:1], mine[1:2])


def test_star_curve_discounts_packages(ctx):
    vals = sorted(ctx.model_value.values(), reverse=True)
    top_vorp = max(ctx.vorp.values())
    half = [p for p, v in ctx.vorp.items() if abs(v - top_vorp / 2) < top_vorp * 0.05]
    if half:
        assert 2 * ctx.model_value[half[0]] < vals[0]


def test_search_handles_nicknames(ctx):
    assert ctx.players[ctx.find_player("JSN")]["name"] == "Jaxon Smith-Njigba"
    assert ctx.players[ctx.find_player("Steelers")]["pos"] == "DEF"


def test_stop_ends_a_running_ai_process():
    """The chat's Stop button sets this flag; the AI process should end right away."""
    import threading
    import time
    from huddle import cli_agents

    cancel = threading.Event()
    seen = []
    script = "import json, time\nfor i in range(100):\n    print(json.dumps({'n': i}), flush=True); time.sleep(0.1)\n"
    threading.Timer(0.4, cancel.set).start()
    start = time.time()
    res = cli_agents._stream([sys.executable, "-c", script], {}, seen.append, cancel)
    assert time.time() - start < 3
    assert 0 < len(seen) < 100 and res["code"] != 0


def test_trade_value_discounts_waiver_level_depth(ctx):
    """Two players barely better than the waiver wire don't add up to a star."""
    from huddle.trades import TradeEngine, WAIVER_FLOOR
    eng = TradeEngine(ctx)
    wr = sorted((p for p in ctx.players if ctx.players[p]["pos"] == "WR"), key=lambda p: -ctx.value[p])
    star, depth = wr[0], [p for p in wr if ctx.value[p] <= eng.waiver_level["WR"]][:2]
    assert eng.trade_value(star) > ctx.value[star] - eng.waiver_level["WR"] - 1e-9
    for p in depth:
        assert abs(eng.trade_value(p) - WAIVER_FLOOR * ctx.value[p]) < 1e-9
    if len(depth) == 2:
        assert eng.market(depth) < eng.market([star])


def test_nobody_sells_their_best_player_to_get_worse():
    from huddle.trades import _acceptance
    mk = {"ratio": 1.15, "need": 1.10, "you_get_best": True, "best_player": "Star", "why": ""}
    assert _acceptance(mk, -0.4)["call"] == "Unlikely"          # fair-ish value, but they get worse
    assert _acceptance(mk, 0.3)["call"] == "Likely"             # fair and it helps them
    assert _acceptance({**mk, "ratio": 1.40}, -0.3)["call"] == "Possible"   # a clear overpay
    # the side getting the best player can take a small lineup hit
    mk2 = {"ratio": 1.0, "need": 0.95, "you_get_best": False, "best_player": "Star", "why": ""}
    assert _acceptance(mk2, -0.3)["call"] == "Possible"
    assert _acceptance({**mk2, "ratio": 0.80}, 0.0)["call"] == "Unlikely"


def test_backtest_runs_on_the_fixture_league(ctx):
    from huddle import backtest
    r = backtest.run(ctx)
    assert r["ready"] and r["weeks"] and r["weeks"][0] == backtest.FIRST_WEEK and r["weeks"][-1] < ctx.week
    acc = r["accuracy"]
    assert acc["n"] > 100 and {"huddle", "sleeper", "model", "naive"} <= set(acc["methods"])
    for m in acc["methods"].values():
        assert 0 < m["mae"] < 20 and 0 <= m["head_to_head"] <= 100
    assert acc["blend"]["best"] in backtest.BLENDS
    lu = r["lineups"]
    assert lu["available"] and lu["n"] == len(r["weeks"])
    for w in lu["weeks"]:
        # nobody beats the best possible lineup in hindsight
        assert w["huddle"] <= w["best"] + 1e-6 and w["sleeper"] <= w["best"] + 1e-6
    assert r["summary"]


def test_backtest_never_sees_the_future(ctx):
    """Scramble every stat from the replayed week onward: the projections for that week
    must not change at all."""
    import copy
    from huddle.backtest import Backtest
    week = 6
    bt = Backtest(ctx)
    pids = sorted(pid for pid, rows in ctx.history.items() if any(r["week"] < week for r in rows))[:300]
    before = bt.project_week(week, pids, {}, ctx.dvp_table(before_week=week))
    saved = (ctx.history, ctx.weekly_pts)
    try:
        ctx.history = copy.deepcopy(ctx.history)
        for rows in ctx.history.values():
            for r in rows:
                if r["week"] >= week:
                    r["pts"] = 999.0
        ctx.weekly_pts = [(o, p, w, 999.0 if w >= week else pts, g) for o, p, w, pts, g in ctx.weekly_pts]
        bt2 = Backtest(ctx)
        after = bt2.project_week(week, pids, {}, ctx.dvp_table(before_week=week))
    finally:
        ctx.history, ctx.weekly_pts = saved
    for pid in pids:
        for k in ("model", "naive", "avail"):
            assert before[pid][k] == after[pid][k], (pid, k)
