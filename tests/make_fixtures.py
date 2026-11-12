"""Build an offline mock Sleeper league from real nflverse data, for testing.

    python tests/make_fixtures.py <nflverse_csv_dir> <out_dir> [--with-projections]

Creates a 10-team full-PPR league (QB, 2 RB, 3 WR, TE, 2 FLEX, K, DEF, 4 bench),
snake-drafted from 2026 production, and writes Sleeper-shaped JSON files that
huddle.sleeper.Sleeper reads when HUDDLE_FIXTURES is set.
"""
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from huddle.sleeper import API, API2, FANTASY_POSITIONS, fixture_name  # noqa: E402
from huddle.teams import TEAM_NAMES, canon  # noqa: E402

SLEEPER_CODE = {"LA": "LAR"}
SEASON, WEEK = 2026, 10

PPR = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 1, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2, "fum_rec_td": 6,
    "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50p": 5, "fgmiss": -1, "xpm": 1, "xpmiss": -1,
    "sack": 1, "int": 2, "fum_rec": 2, "def_td": 6, "safe": 2, "blk_kick": 2, "st_td": 6, "ff": 1,
    "pts_allow_0": 10, "pts_allow_1_6": 7, "pts_allow_7_13": 4, "pts_allow_14_20": 1,
    "pts_allow_21_27": 0, "pts_allow_28_34": -1, "pts_allow_35p": -4,
}
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF", "BN", "BN", "BN", "BN"]


def write(out: Path, url: str, data) -> None:
    (out / fixture_name(url)).write_text(json.dumps(data))


def main(src: Path, out: Path, with_proj: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    stats = list(csv.DictReader(open(src / "stats_player_week_2026.csv")))
    ids = list(csv.DictReader(open(src / "db_playerids.csv")))
    inj = list(csv.DictReader(open(src / "injuries_2026.csv")))
    gsis_to_sleeper = {r["gsis_id"]: r for r in ids if r["sleeper_id"] not in ("", "NA") and r["gsis_id"] not in ("", "NA")}
    totals = defaultdict(float)
    games = defaultdict(int)
    last_team = {}
    for r in stats:
        if r["position"] in ("QB", "RB", "WR", "TE", "K"):
            totals[r["player_id"]] += float(r["fantasy_points_ppr"] or 0) if r["position"] != "K" else \
                3 * float(r["fg_made"] or 0) + float(r["pat_made"] or 0)
            games[r["player_id"]] += 1
            last_team[r["player_id"]] = (int(r["week"]), r["team"], r["position"])
    latest_inj = {}
    for r in inj:
        if r["week"] == "9" and r["report_status"]:
            latest_inj[r["gsis_id"]] = r
    players = {}
    ranked = sorted(totals, key=lambda g: -totals[g])
    for rank, g in enumerate(ranked, start=1):
        meta = gsis_to_sleeper.get(g)
        if not meta:
            continue
        _, team, pos = last_team[g]
        sid = meta["sleeper_id"]
        first, _, last = meta["name"].partition(" ")
        status = None
        if g in latest_inj:
            status = {"Out": "Out", "Doubtful": "Doubtful", "Questionable": "Questionable"}.get(latest_inj[g]["report_status"])
        players[sid] = {
            "player_id": sid, "first_name": first, "last_name": last, "full_name": meta["name"],
            "position": pos, "fantasy_positions": [pos], "team": SLEEPER_CODE.get(canon(team), canon(team)),
            "status": "Active", "active": True, "gsis_id": " " + g, "age": 25, "years_exp": 3,
            "search_rank": rank, "injury_status": status,
            "injury_body_part": latest_inj[g]["report_primary_injury"] if g in latest_inj else None,
        }
    for t in TEAM_NAMES:
        code = SLEEPER_CODE.get(t, t)
        players[code] = {"player_id": code, "first_name": t, "last_name": TEAM_NAMES[t], "position": "DEF",
                         "fantasy_positions": ["DEF"], "team": code, "active": True, "search_rank": 400}
    write(out, f"{API}/players/nfl", players)
    write(out, f"{API}/state/nfl", {"season": str(SEASON), "league_season": str(SEASON), "week": WEEK,
                                       "season_type": "regular"})
    write(out, f"{API}/user/testuser", {"user_id": "u1", "username": "testuser", "display_name": "Tester"})
    write(out, f"{API}/user/u1/leagues/nfl/{SEASON}", [{"league_id": "L1", "name": "Fixture League"}])
    write(out, f"{API}/league/L1", {
        "league_id": "L1", "name": "Fixture League", "season": str(SEASON), "total_rosters": 10,
        "scoring_settings": PPR, "roster_positions": ROSTER_POSITIONS,
        "settings": {"playoff_week_start": 15, "playoff_teams": 6, "num_teams": 10, "reserve_slots": 1},
    })
    users = [{"user_id": f"u{i}", "display_name": f"Manager{i}", "metadata": {"team_name": f"Team {i}"}} for i in range(1, 11)]
    users[0]["metadata"]["team_name"] = "Tester's Team"
    write(out, f"{API}/league/L1/users", users)

    # snake draft with positional caps; DEF/K late
    caps = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "K": 1, "DEF": 1}
    pool = [sid for sid in sorted(players, key=lambda s: players[s]["search_rank"])
            if players[sid]["position"] in ("QB", "RB", "WR", "TE")]
    k_pool = [sid for sid in sorted(players, key=lambda s: players[s]["search_rank"]) if players[sid]["position"] == "K"]
    def_pool = [t for t in players if players[t]["position"] == "DEF"]
    rng.shuffle(def_pool)
    rosters = {i: [] for i in range(1, 11)}
    taken = set()
    for rnd in range(13):
        order = range(1, 11) if rnd % 2 == 0 else range(10, 0, -1)
        for rid in order:
            counts = defaultdict(int)
            for p in rosters[rid]:
                counts[players[p]["position"]] += 1
            # a little randomness so teams differ
            options = [p for p in pool if p not in taken and counts[players[p]["position"]] < caps[players[p]["position"]]][:3]
            pick = options[0] if rng.random() < 0.7 else rng.choice(options)
            rosters[rid].append(pick)
            taken.add(pick)
    for rid in rosters:
        rosters[rid].append(k_pool.pop(0))
        rosters[rid].append(def_pool.pop(0))
    rows = []
    for rid, plist in rosters.items():
        wins = rng.randint(2, 7)
        rows.append({"roster_id": rid, "owner_id": f"u{rid}", "players": plist, "starters": plist[:11],
                     "reserve": [], "taxi": [], "settings": {"wins": wins, "losses": 9 - wins, "ties": 0,
                                                              "fpts": rng.randint(900, 1300), "fpts_decimal": 50,
                                                              "fpts_against": rng.randint(900, 1300)}})
    write(out, f"{API}/league/L1/rosters", rows)
    write(out, f"{API}/league/L1/matchups/{WEEK}",
          [{"roster_id": rid, "matchup_id": (rid + 1) // 2, "points": 0, "starters": rosters[rid][:11]} for rid in rosters])
    fas = [p for p in pool if p not in taken][:8]
    write(out, f"{API}/players/nfl/trending/add?lookback_hours=48&limit=100",
          [{"player_id": p, "count": 5000 - 400 * i} for i, p in enumerate(fas)])

    if with_proj:
        # Past weeks, for the backtest: Sleeper-style projections made from each player's
        # average BEFORE that week (plus noise), and matchups where each manager starts what
        # looked best beforehand, scored with what actually happened.
        pts_by_week = defaultdict(dict)       # sleeper id -> {week: actual PPR points}
        for r in stats:
            meta = gsis_to_sleeper.get(r["player_id"])
            if meta and r["position"] in ("QB", "RB", "WR", "TE"):
                pts_by_week[meta["sleeper_id"]][int(r["week"])] = float(r["fantasy_points_ppr"] or 0)
        pos_q = "&".join(f"position[]={p}" for p in FANTASY_POSITIONS)
        for w in range(1, WEEK):
            lst, pre = [], {}
            for sid, wk in pts_by_week.items():
                prior = [v for ww, v in wk.items() if ww < w]
                if not prior or sid not in players:
                    continue
                v = sum(prior) / len(prior) * rng.uniform(0.8, 1.2)
                pre[sid] = v
                pos = players[sid]["position"]
                st = {"pass_yd": v * 12, "pass_td": v / 10, "rush_yd": v} if pos == "QB" else \
                     {"rec": v / 4, "rec_yd": v * 3, "rec_td": v / 40, "rush_yd": v if pos == "RB" else 0}
                lst.append({"player_id": sid, "stats": st, "week": w})
            write(out, f"{API2}/projections/nfl/{SEASON}/{w}?season_type=regular&{pos_q}", lst)
            rows_w = []
            for rid, plist in rosters.items():
                order = sorted(plist, key=lambda p: -(pre.get(p, 0) * rng.uniform(0.7, 1.3)))
                starters, used = [], defaultdict(int)
                limits = {"QB": 1, "RB": 3, "WR": 4, "TE": 2}
                for pid in order:
                    pos = players[pid]["position"]
                    if pos in limits and used[pos] < limits[pos] and len(starters) < 9:
                        starters.append(pid)
                        used[pos] += 1
                starters += [p for p in plist if players[p]["position"] in ("K", "DEF")]
                pp = {pid: pts_by_week.get(pid, {}).get(w, 0.0) for pid in plist}
                rows_w.append({"roster_id": rid, "matchup_id": (rid + 1) // 2, "players": plist, "starters": starters,
                               "players_points": pp, "points": round(sum(pp[p] for p in starters), 2)})
            write(out, f"{API}/league/L1/matchups/{w}", rows_w)

        # fake Sleeper projections (list format) from season averages, for weeks 10-17
        avg = {}
        for g in totals:
            meta = gsis_to_sleeper.get(g)
            if meta and games[g]:
                avg[meta["sleeper_id"]] = totals[g] / games[g]
        pos_q = "&".join(f"position[]={p}" for p in FANTASY_POSITIONS)
        for w in range(WEEK, 18):
            lst = []
            for sid, a in avg.items():
                pos = players[sid]["position"]
                v = a * rng.uniform(0.85, 1.15)
                if pos == "QB":
                    st = {"pass_yd": v * 12, "pass_td": v / 10, "rush_yd": v}
                elif pos == "K":
                    st = {"fgm_30_39": v / 6, "xpm": v / 3}
                else:
                    st = {"rec": v / 4, "rec_yd": v * 3, "rec_td": v / 40, "rush_yd": v if pos == "RB" else 0}
                lst.append({"player_id": sid, "stats": st, "week": w})
            write(out, f"{API2}/projections/nfl/{SEASON}/{w}?season_type=regular&{pos_q}", lst)
    print(f"wrote fixtures to {out}: {len(players)} players, mine = {[players[p]['full_name'] if 'full_name' in players[p] else p for p in rosters[1]]}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), "--with-projections" in sys.argv)
