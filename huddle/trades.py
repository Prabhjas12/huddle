"""Trade analyzer and trade finder.

The core idea: a trade is only as good as what it does to the lineup you can
actually start, week by week, for the rest of the season. So instead of adding
up player values, Huddle

  1. re-solves both teams' optimal lineups for every remaining week (byes and
     injuries included) before and after the deal,
  2. makes the team receiving more players cut its least useful bench player
     (roster limits are real), and lets the team receiving fewer players pick
     up the best free agent with the open spot,
  3. weights the fantasy-playoff weeks more heavily.

That automatically discounts 2-for-1 packages: two good players only help if
they would both start.

Whether the other manager says yes is judged the way a manager actually
thinks about it:

  * Trade value, not raw value. A player is only worth what he adds over the
    best players sitting on your league's waiver wire. In a small league the
    wire is deep, so two decent starters can add up to very little, and nobody
    gives up a star for them.
  * Players they'd have to cut to make room count against the deal.
  * The team giving up the best player wants clearly more back, and almost
    never does it if its own lineup gets worse.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .lineup import Lineup, season_totals
from .model import LeagueContext

DEDICATED = ("QB", "RB", "WR", "TE", "K", "DEF")

# trade grading thresholds
MIN_GAIN_PW = 0.5          # a suggested trade must add at least this many lineup points per week for you
LIKELY_THEIR_PW = -0.1     # their lineup can't get worse than this for "likely"
POSSIBLE_THEIR_PW = -0.5   # ...or this for "possible"
POSSIBLE_SLACK = 0.08      # "possible" allows value up to 8% short of what they'd need, if it helps their lineup
SELL_STAR_PREMIUM = 0.25   # giving up the best player AND getting worse now takes a clear overpay on top of that
MAX_OVERPAY = 1.35         # never suggest giving more than 1.35x the trade value you get
WAIVER_FLOOR = 0.25        # a player no better than the waiver wire still keeps 25% of his value (he's on a roster)
VERIFY_TOP = 30            # the finder double-checks this many of its best ideas with the full analysis


class TradeEngine:
    def __init__(self, ctx: LeagueContext):
        self.ctx = ctx
        self.pos = ctx.positions_map()
        self.n_weeks = len(ctx.ros_weeks)
        self.required = {p: sum(1 for s in ctx.slots if s == p) for p in DEDICATED}
        fa_sorted = sorted(ctx.free_agents, key=lambda p: -ctx.ros_total.get(p, 0.0))
        self.top_fa: Dict[str, List[str]] = {p: [] for p in DEDICATED}
        for pid in fa_sorted:
            lst = self.top_fa.get(ctx.players[pid]["pos"])
            if lst is not None and len(lst) < 3:
                lst.append(pid)
        # What anyone in the league can get for free: the value of the second-best free agent
        # (by rest-of-season points), so one lucky add doesn't set it. RB, WR and TE share FLEX
        # spots and managers don't price positions that differently, so each of those is
        # half its own wire and half the best of the three.
        own: Dict[str, float] = {}
        for pos, lst in self.top_fa.items():
            vals = sorted((ctx.value.get(p, 0.0) for p in lst), reverse=True)
            own[pos] = vals[min(1, len(vals) - 1)] if vals else 0.0
        flex = max(own.get(p, 0.0) for p in ("RB", "WR", "TE"))
        self.waiver_level: Dict[str, float] = {
            pos: (0.5 * v + 0.5 * flex) if pos in ("RB", "WR", "TE") else v for pos, v in own.items()}

    # -------------------------------------------------------------- helpers
    # The team being evaluated: players on its IR slots still score once they're back,
    # and an open IR slot can make room for an injured player instead of a cut.
    _extra: List[str] = []
    _ir_open: int = 0

    def _scope(self, rid: int, exclude: Sequence[str] = ()) -> None:
        t = self.ctx.team(rid)
        self._extra = [p for p in t["reserve"] if p in self.ctx.players and p not in exclude]
        self._ir_open = self.ctx.open_ir_slots(rid)

    def totals(self, pids: Sequence[str]) -> Tuple[float, np.ndarray]:
        pids = list(pids) + [p for p in self._extra if p not in pids]
        return season_totals(self.ctx.slots, pids, self.pos, self.ctx.proj, self.ctx.weights)

    def keep_score(self, pid: str) -> float:
        """How much a team would hate to cut a player. Long-term view: rest-of-season points
        (injured players count from when they return), what other managers would pay for him,
        and a hard floor for anyone the consensus still ranks as a real asset."""
        ctx = self.ctx
        score = ctx.ros_total.get(pid, 0.0) + 0.6 * ctx.value.get(pid, 0.0)
        o = ctx.injury_outlook(ctx.players[pid])
        rank = ctx.consensus.get(pid, {}).get("rank")
        if rank is not None and rank <= 150 and not (o and o["season_ending"]):
            score += 40.0
        return score

    def is_asset(self, pid: str) -> bool:
        """Someone a manager wouldn't cut just to stream a player: the consensus still ranks him
        in the top 150 and he isn't out for the season (same rule as the Waivers page)."""
        rank = self.ctx.consensus.get(pid, {}).get("rank")
        o = self.ctx.injury_outlook(self.ctx.players[pid])
        return rank is not None and rank <= 150 and not (o and o["season_ending"])

    def _drop_candidate(self, pids: List[str], protected: set) -> Optional[str]:
        ctx = self.ctx
        counts: Dict[str, int] = {}
        for p in pids:
            counts[ctx.players[p]["pos"]] = counts.get(ctx.players[p]["pos"], 0) + 1
        ok = [p for p in pids if p not in protected
              and counts[ctx.players[p]["pos"]] - 1 >= self.required.get(ctx.players[p]["pos"], 0)]
        pool = ok or [p for p in pids if p not in protected] or pids
        return min(pool, key=self.keep_score)

    def _ir_candidate(self, pids: List[str], protected: set) -> Optional[str]:
        if self._ir_open <= 0:
            return None
        elig = [p for p in pids if p not in protected and self.ctx.ir_eligible(p)]
        return max(elig, key=self.keep_score) if elig else None

    def settle(self, pids: List[str], protected: set, open_spots: int, exclude: set,
               thorough: bool = True, hint_positions: Sequence[str] = ()) -> Tuple[List[str], List[str], List[str]]:
        """Apply roster limits. Returns (roster, dropped, added)."""
        pids = list(pids)
        drops: List[str] = []
        adds: List[str] = []
        while len(pids) > self.ctx.roster_limit:
            ir = self._ir_candidate(pids, protected)
            if ir:                       # use an open IR slot before cutting anyone
                pids.remove(ir)
                self._extra = self._extra + [ir]
                self._ir_open -= 1
                continue
            d = self._drop_candidate(pids, protected)
            pids.remove(d)
            drops.append(d)
        spots = min(open_spots, self.ctx.roster_limit - len(pids))
        for _ in range(max(spots, 0)):
            if thorough:
                cands = [p for pos in DEDICATED for p in self.top_fa[pos][:2] if p not in exclude and p not in adds]
            else:
                cands = [p for pos in (hint_positions or DEDICATED) for p in self.top_fa.get(pos, [])[:1]
                         if p not in exclude and p not in adds]
            if not cands:
                break
            base, _ = self.totals(pids)
            best, best_gain = None, 0.0
            for c in cands:
                tot, _ = self.totals(pids + [c])
                if tot - base > best_gain:
                    best, best_gain = c, tot - base
            if best is None:
                break
            pids.append(best)
            adds.append(best)
        return pids, drops, adds

    def waiver_move(self, pids: List[str], exclude: set, thorough: bool = True) -> Tuple[List[str], Optional[str], Optional[str]]:
        """Best single drop-and-add from the waiver wire (or None if nothing helps).

        Applied to both the before and after rosters so a trade is never credited
        for a pickup you could make anyway.
        """
        per_pos = 2 if thorough else 1
        cands = [p for pos in DEDICATED for p in self.top_fa[pos][:per_pos + 1] if p not in exclude][: per_pos * 6]
        base, _ = self.totals(pids)
        best = (base + 0.5, None, None, pids)
        for c in cands:
            new = list(pids) + [c]
            drop = None
            if len(new) > self.ctx.roster_limit:
                drop = self._drop_candidate(new, {c})
                if self.is_asset(drop):     # nobody cuts a real asset for a streamer
                    continue
                new.remove(drop)
            tot, _ = self.totals(new)
            if tot > best[0]:
                best = (tot, c, drop, new)
        return best[3], best[1], best[2]

    def waiver_moves(self, pids: List[str], exclude: set, thorough: bool = True,
                     max_moves: int = 3) -> Tuple[List[str], List[str], List[str]]:
        """Repeat the best drop-and-add until nothing on the wire helps."""
        adds: List[str] = []
        drops: List[str] = []
        for _ in range(max_moves):
            pids, add, drop = self.waiver_move(pids, exclude | set(adds), thorough)
            if not add:
                break
            adds.append(add)
            if drop:
                drops.append(drop)
        return pids, adds, drops

    def trade_value(self, pid: str) -> float:
        """What a player is worth in a trade in THIS league: his market value (expert consensus
        blended with Huddle's projections) above what the waiver wire offers at his position.
        A WR barely better than the best free-agent WR is worth little, however good his name."""
        v = self.ctx.value.get(pid, 0.0)
        w = self.waiver_level.get(self.ctx.players[pid]["pos"], 0.0)
        return max(v - w, 0.0) + WAIVER_FLOOR * min(v, w)

    def market(self, pids: Sequence[str]) -> float:
        return round(sum(self.trade_value(p) for p in pids), 1)

    def market_check(self, give: Sequence[str], get: Sequence[str],
                     their_drops: Sequence[str] = ()) -> Dict[str, Any]:
        """How the other manager sees the deal, in trade value.

        The team that gets the best player will accept giving up a little more;
        the team that gives up the best player wants clearly more back. Anyone
        they'd have to cut to make room is subtracted from what they get.
        """
        ctx = self.ctx
        cut = self.market(their_drops)
        they_get, they_give = round(self.market(give) - cut, 1), self.market(get)
        best = max(list(give) + list(get), key=lambda p: ctx.value.get(p, 0.0))
        best_name = ctx.players[best]["name"]
        if best in give:
            need = 0.95
            why = f"They'd get the best player in the deal ({best_name}), so they can accept giving up a little more."
        elif len(give) > len(get):
            need = 1.10
            why = f"They'd give up the best player ({best_name}) for a package, so they'll want clearly more back."
        elif len(get) > len(give):
            need = 1.15
            why = f"They'd give up the best player ({best_name}) and an extra player, which almost nobody does without a big overpay."
        else:
            need = 1.00
            why = f"Straight swap; they'll want at least equal value for {best_name}."
        if cut:
            why += " Making room would cost them " + ", ".join(ctx.players[p]["name"] for p in their_drops) + "."
        ratio = they_get / they_give if they_give > 0 else (9.99 if they_get > 0 else 0.0)
        return {"they_get": they_get, "they_give": they_give, "ratio": round(ratio, 2), "need": need,
                "best_player": best_name, "you_get_best": best in get, "why": why,
                "they_cut": [ctx.players[p]["name"] for p in their_drops]}

    # ------------------------------------------------------------- analyzer
    def analyze(self, give: Sequence[str], get: Sequence[str], partner: Optional[int] = None,
                thorough: bool = True, _base: Optional[Dict[int, Tuple[List[str], float, np.ndarray]]] = None) -> Dict[str, Any]:
        ctx = self.ctx
        me = ctx.my_roster_id
        give, get = [str(p) for p in give], [str(p) for p in get]
        if partner is None:
            owners = {ctx.owner_of.get(p) for p in get}
            if len(owners) != 1 or None in owners:
                raise ValueError("All players you receive must come from one other team.")
            partner = owners.pop()
        if partner == me:
            raise ValueError("Pick players from another team for the 'you get' side.")
        mine, theirs = ctx.team(me)["players"], ctx.team(partner)["players"]
        bad = [ctx.players[p]["name"] for p in give if p not in mine] + \
              [ctx.players[p]["name"] for p in get if p not in theirs]
        if bad:
            raise ValueError("Not on the right roster: " + ", ".join(bad))

        exclude = set(ctx.owner_of)
        sides = {}
        for rid, out, inn in ((me, give, get), (partner, get, give)):
            self._scope(rid, exclude=out)
            before = ctx.active_roster(rid)
            if _base and rid in _base:
                base_roster, b_tot, b_week = _base[rid]
            else:
                base_roster, _, _ = self.waiver_moves(before, exclude, thorough, 3 if thorough else 2)
                b_tot, b_week = self.totals(base_roster)
            after = [p for p in before if p not in out] + inn
            out_active = [p for p in out if p in before]
            opened = len(out_active) - len(inn)
            hint = [ctx.players[p]["pos"] for p in out]
            after, drops, adds = self.settle(after, set(inn), opened, exclude, thorough, hint)
            after, w_adds, w_drops = self.waiver_moves(after, exclude | set(adds), thorough, 3 if thorough else 2)
            # report only the roster moves the trade itself causes (vs the baseline):
            # players this team owns that it would have to cut, and free agents it would add
            adds = [p for p in after if p not in base_roster and p not in inn and p not in before]
            drops = [p for p in base_roster if p not in after and p not in out and p in before]
            exclude |= set(adds)
            a_tot, a_week = self.totals(after)
            sides[rid] = {"before": before, "baseline": base_roster, "after": after, "drops": drops, "adds": adds,
                          "b_tot": b_tot, "a_tot": a_tot, "b_week": b_week, "a_week": a_week,
                          "out": out, "in": inn}
        return self._report(sides[me], sides[partner], partner, thorough)

    def baseline(self, rid: int, thorough: bool = True) -> Tuple[List[str], float, np.ndarray]:
        self._scope(rid)
        roster, _, _ = self.waiver_moves(self.ctx.active_roster(rid), set(self.ctx.owner_of), thorough,
                                         3 if thorough else 2)
        tot, week = self.totals(roster)
        return roster, tot, week

    def _report(self, me: Dict[str, Any], them: Dict[str, Any], partner: int, thorough: bool) -> Dict[str, Any]:
        ctx = self.ctx
        weeks = ctx.ros_weeks
        n = max(len(weeks), 1)
        my_delta = me["a_tot"] - me["b_tot"]
        their_delta = them["a_tot"] - them["b_tot"]
        # only players already on their roster count as a cost (not waiver pickups Huddle assumed)
        their_cuts = [p for p in them["drops"] if p in them["before"]]
        mk = self.market_check(me["out"], me["in"], their_cuts)
        give_val, get_val = mk["they_get"], mk["they_give"]
        diff_pct = (get_val - give_val) / max(give_val, get_val, 1.0)
        out = {
            "partner": {"roster_id": partner, "name": ctx.teams[partner]["name"]},
            "give": [ctx.brief(p) for p in me["out"]],
            "get": [ctx.brief(p) for p in me["in"]],
            "you": {
                "ros_delta": round(my_delta, 1), "per_week_delta": round(my_delta / n, 2),
                "this_week_delta": round(float(me["a_week"][0] - me["b_week"][0]), 1),
                "drops": [ctx.brief(p) for p in me["drops"]], "adds": [ctx.brief(p) for p in me["adds"]],
                "assumed_pickups": [ctx.players[p]["name"] for p in me["baseline"] if p not in me["before"]],
            },
            "them": {
                "ros_delta": round(their_delta, 1), "per_week_delta": round(their_delta / n, 2),
                "drops": [ctx.brief(p) for p in them["drops"]], "adds": [ctx.brief(p) for p in them["adds"]],
            },
            "market": {"you_give": give_val, "you_get": get_val, "diff_pct": round(100 * diff_pct, 1),
                       "ratio": mk["ratio"], "need": mk["need"], "best_player": mk["best_player"],
                       "you_get_best": mk["you_get_best"], "why": mk["why"], "they_cut": mk["they_cut"],
                       "waiver_level": {p: round(v, 1) for p, v in self.waiver_level.items() if p not in ("K", "DEF")}},
        }
        out["verdict"] = _verdict(my_delta / n)
        out["acceptance"] = _acceptance(mk, their_delta / n)
        out["grading"] = _grading(my_delta / n, their_delta / n, mk)
        if not thorough:
            return out
        out["weeks"] = [
            {"week": w, "before": round(float(b), 1), "after": round(float(a), 1),
             "delta": round(float(a - b), 1), "playoffs": w >= ctx.playoff_week_start}
            for w, b, a in zip(weeks, me["b_week"], me["a_week"])
        ]
        # starting lineup this week, before and after
        out["lineup_now"] = self._lineup_names(me["baseline"])
        out["lineup_after"] = self._lineup_names(me["after"])
        out["notes"] = self._notes(me, them, out)
        return out

    def _lineup_names(self, pids: Sequence[str]) -> List[Dict[str, Any]]:
        lu = Lineup(self.ctx.slots, list(pids), self.pos)
        pts = np.array([self.ctx.week_proj(p) for p in lu.pids]) if lu.pids else np.zeros(0)
        _, assigned = lu.solve(pts)
        return [{"slot": s, "name": self.ctx.players[p]["name"] if p else "(empty)", "proj": round(v, 1),
                 "free_agent": bool(p) and p not in self.ctx.owner_of}
                for s, p, v in assigned]

    def _notes(self, me: Dict[str, Any], them: Dict[str, Any], out: Dict[str, Any]) -> List[str]:
        ctx = self.ctx
        notes: List[str] = []
        n_in, n_out = len(me["in"]), len(me["out"])
        best_in = max((ctx.value.get(p, 0) for p in me["in"]), default=0)
        best_out = max((ctx.value.get(p, 0) for p in me["out"]), default=0)
        if n_out > n_in:
            notes.append(f"You consolidate {n_out} players into {n_in}" +
                         (" and get the best player in the deal." if best_in >= best_out else ", but don't get the best player in the deal."))
        elif n_in > n_out:
            notes.append(f"You take on {n_in} players for {n_out}; extra depth only counts when it starts.")
        if me["drops"]:
            notes.append("To make room you'd drop " + ", ".join(ctx.players[p]["name"] for p in me["drops"]) + ".")
        if me["adds"]:
            notes.append("The roster spot this opens goes to " +
                         ", ".join(ctx.players[p]["name"] for p in me["adds"]) + " off waivers.")
        weeks = out["weeks"]
        hurt = [w for w in weeks if w["delta"] <= -3]
        helped = [w for w in weeks if w["delta"] >= 3]
        if hurt:
            notes.append("Weaker lineup in " + _weeks_text(hurt, len(weeks)) + " - check byes and injuries.")
        if helped:
            notes.append("Stronger lineup in " + _weeks_text(helped, len(weeks)) + ".")
        po = [w for w in out["weeks"] if w["playoffs"]]
        if po:
            d = sum(w["delta"] for w in po)
            notes.append(f"Fantasy playoffs (weeks {po[0]['week']}-{po[-1]['week']}): {d:+.1f} lineup points.")
        mk = out["market"]
        if mk["ratio"] >= 1.25:
            notes.append(f"By trade value you'd give up about {mk['ratio']:.1f}x what you get. That's fine if the lineup "
                         "gain is what you want, but you may be able to ask for more.")
        inj = [ctx.players[p]["name"] + f" ({ctx.players[p]['injury_status']})"
               for p in me["in"] if ctx.players[p].get("injury_status")]
        if inj:
            notes.append("Injury risk on players you'd receive: " + ", ".join(inj) + ".")
        return notes

    # --------------------------------------------------------------- finder
    def find(self, focus_pos: Optional[str] = None, max_results: int = 10,
             partner: Optional[int] = None) -> List[Dict[str, Any]]:
        ctx = self.ctx
        me = ctx.my_roster_id
        focus_pos = focus_pos.upper() if focus_pos else None
        skip_pos = set() if focus_pos in ("K", "DEF") else {"K", "DEF"}
        mine = [p for p in ctx.active_roster(me) if ctx.players[p]["pos"] not in skip_pos and ctx.value.get(p, 0) > 0.5]
        mine.sort(key=lambda p: -ctx.value.get(p, 0))
        base = {me: self.baseline(me, thorough=False)}
        results = []
        for rid in ctx.teams:
            if rid == me or (partner and rid != partner):
                continue
            theirs = [p for p in ctx.active_roster(rid) if ctx.players[p]["pos"] not in skip_pos and ctx.value.get(p, 0) > 0.5]
            theirs.sort(key=lambda p: -ctx.value.get(p, 0))
            base[rid] = self.baseline(rid, thorough=False)
            packages = []
            for g in mine[:12]:
                for t in theirs[:10]:
                    packages.append(([g], [t]))
            for g1, g2 in combinations(mine[:9], 2):
                for t in theirs[:8]:
                    packages.append(([g1, g2], [t]))
            for g in mine[:8]:
                for t1, t2 in combinations(theirs[:9], 2):
                    packages.append(([g], [t1, t2]))
            for give, get in packages:
                if focus_pos and not any(ctx.players[p]["pos"] == focus_pos for p in get):
                    continue
                mk = self.market_check(give, get)
                # cheap screens first: the other manager must see fair value, and you shouldn't overpay badly
                if mk["they_give"] < 3 or mk["ratio"] < mk["need"] - POSSIBLE_SLACK or mk["ratio"] > MAX_OVERPAY:
                    continue
                r = self.analyze(give, get, rid, thorough=False, _base=base)
                you_pw, them_pw = r["you"]["per_week_delta"], r["them"]["per_week_delta"]
                if you_pw < MIN_GAIN_PW - 0.2 or r["acceptance"]["call"] == "Unlikely":
                    continue
                r["score"] = round(you_pw + 0.3 * max(min(them_pw, 2.0), -1.0), 2)
                results.append(r)
        # The search above uses a quick replay. Re-run the most promising ideas with the full
        # analysis (the same one you see when you open a trade) so the labels always match.
        results.sort(key=lambda r: -r["score"])
        checked = []
        for r in results[:VERIFY_TOP]:
            full = self.analyze([p["id"] for p in r["give"]], [p["id"] for p in r["get"]], r["partner"]["roster_id"])
            you_pw, them_pw = full["you"]["per_week_delta"], full["them"]["per_week_delta"]
            if you_pw < MIN_GAIN_PW or full["acceptance"]["call"] == "Unlikely":
                continue
            full["score"] = round(you_pw + 0.3 * max(min(them_pw, 2.0), -1.0), 2)
            checked.append(full)
        results = checked
        # trades they'd actually take come first; within each group, the biggest gain for you
        results.sort(key=lambda r: (r["acceptance"]["call"] != "Likely", -r["score"]))
        # keep variety: at most two ideas per target player
        picked, seen = [], {}
        for r in results:
            key = tuple(sorted(p["id"] for p in r["get"]))
            if seen.get(key, 0) >= 2:
                continue
            seen[key] = seen.get(key, 0) + 1
            picked.append(r)
            if len(picked) >= max_results:
                break
        return picked


def _weeks_text(ws: List[Dict[str, Any]], total: int) -> str:
    if len(ws) > 4:
        return f"{len(ws)} of {total} remaining weeks"
    nums = [str(w["week"]) for w in ws]
    return ("week " if len(nums) == 1 else "weeks ") + ", ".join(nums)


def _verdict(per_week: float) -> Dict[str, str]:
    if per_week >= 0.75:
        return {"call": "Accept", "tone": "good", "why": f"Your starting lineup gains {per_week:+.1f} pts a week."}
    if per_week >= 0.25:
        return {"call": "Lean accept", "tone": "good", "why": f"A small lineup gain ({per_week:+.1f} pts a week)."}
    if per_week > -0.25:
        return {"call": "Neutral", "tone": "even", "why": "Barely changes your starting lineup."}
    if per_week > -0.75:
        return {"call": "Lean decline", "tone": "bad", "why": f"Your lineup gets slightly worse ({per_week:+.1f} pts a week)."}
    return {"call": "Decline", "tone": "bad", "why": f"Your starting lineup loses {per_week:+.1f} pts a week."}


def _acceptance(mk: Dict[str, Any], their_pw: float) -> Dict[str, str]:
    """Would the other manager say yes? Both value and their own lineup matter, and the
    team giving up the best player is the hardest to convince."""
    ratio, need = mk["ratio"], mk["need"]
    fair = ratio >= need
    close = ratio >= need - POSSIBLE_SLACK
    sells_best = mk["you_get_best"]          # they'd be giving up the best player in the deal
    if sells_best and their_pw < 0:
        if ratio >= need + SELL_STAR_PREMIUM and their_pw >= POSSIBLE_THEIR_PW:
            return {"call": "Possible", "why": f"They'd give up {mk['best_player']} and get a little worse this season, "
                                               "but the value back is a clear overpay, so a manager thinking long-term might bite."}
        return {"call": "Unlikely", "why": f"They'd give up the best player in the deal ({mk['best_player']}) and their "
                                           "own lineup would get worse. Managers almost never do that."}
    if fair and their_pw >= LIKELY_THEIR_PW:
        why = "It's fair or better for them by trade value"
        why += (", and it improves their lineup too." if their_pw >= 0.25 else ", and it doesn't hurt their lineup.")
        return {"call": "Likely", "why": why}
    if fair and their_pw >= POSSIBLE_THEIR_PW:
        return {"call": "Possible", "why": "Fair by trade value, but it makes their lineup a little worse, so they may hesitate."}
    if close and their_pw >= 0.25:
        return {"call": "Possible", "why": "It helps their lineup, but it's a bit short on value; expect them to ask for a small add-on."}
    if not close:
        return {"call": "Unlikely", "why": "They'd give up clearly more trade value than they get. " + mk["why"]}
    if not fair:
        return {"call": "Unlikely", "why": "It's short on value for them and doesn't help their lineup enough to make up for it."}
    return {"call": "Unlikely", "why": "It noticeably weakens their starting lineup."}


def _grading(you_pw: float, them_pw: float, mk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The checklist shown in the app, so every grade is explainable."""
    return [
        {"test": "Helps your starting lineup", "value": f"{you_pw:+.1f} pts/wk", "pass": you_pw >= MIN_GAIN_PW,
         "rule": f"needs at least +{MIN_GAIN_PW} pts/wk over the rest of the season"},
        {"test": "Doesn't hurt their lineup", "value": f"{them_pw:+.1f} pts/wk", "pass": them_pw >= LIKELY_THEIR_PW,
         "rule": f"{LIKELY_THEIR_PW:+.1f} pts/wk or better for 'likely', {POSSIBLE_THEIR_PW:+.1f} for 'possible'. "
                 "If they give up the best player, their lineup can't get worse at all."},
        {"test": "Fair to them by trade value", "value": f"they get {mk['they_get']:.0f}, give {mk['they_give']:.0f}",
         "pass": mk["ratio"] >= mk["need"],
         "rule": f"Trade value only counts what a player adds over your league's waiver wire. "
                 f"They need at least {mk['need']:.2f}x what they give. {mk['why']}"},
    ]
