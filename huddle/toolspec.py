"""Descriptions of Huddle's tools, shared by every AI provider and the connector.

Kept free of heavy imports so the connector can start instantly.
"""
from typing import Any, Dict, List

TOOLS: List[Dict[str, Any]] = [
    {"name": "get_my_team",
     "description": "Your roster with this week's projection, rest-of-season points per game, trade value, bye week, injury and matchup for every player, plus your record.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_player_report",
     "description": "Full research on one player: injury and practice status, usage (snap %, targets, target share, carries, carry share, WOPR), recent form and game log, home/away splits, next opponent's defense vs his position, Vegas total/spread/implied team total, stadium/roof/surface/weather, projections and upcoming schedule. Call this for every player you give advice about.",
     "parameters": {"type": "object", "properties": {"player": {"type": "string", "description": "Player name, e.g. 'Jahmyr Gibbs' or 'Steelers D/ST'"}},
                    "required": ["player"]}},
    {"name": "compare_players",
     "description": "Side-by-side comparison of 2-5 players for start/sit or trade decisions.",
     "parameters": {"type": "object", "properties": {"players": {"type": "array", "items": {"type": "string"}}},
                    "required": ["players"]}},
    {"name": "get_lineup_advice",
     "description": "Optimal starting lineup for your team for a week, suggested swaps versus the lineup currently set in Sleeper, and free agents to stream into empty or bye-week slots.",
     "parameters": {"type": "object", "properties": {"week": {"type": "integer", "description": "NFL week; defaults to the current week"}}}},
    {"name": "analyze_trade",
     "description": "Evaluate a trade: re-solves both teams' best lineups for every remaining week (byes, injuries, roster limits, waiver replacements, fantasy-playoff weighting) and reports lineup-point impact for both sides, market value and whether the other manager is likely to accept. 'get' players must all be on one other team.",
     "parameters": {"type": "object", "properties": {
         "give": {"type": "array", "items": {"type": "string"}, "description": "Players you send"},
         "get": {"type": "array", "items": {"type": "string"}, "description": "Players you receive"}},
         "required": ["give", "get"]}},
    {"name": "find_trades",
     "description": "Search the league for 1-for-1, 2-for-1 and 1-for-2 trades that improve your lineup and are realistic for the other manager.",
     "parameters": {"type": "object", "properties": {
         "position": {"type": "string", "description": "Only trades that bring back this position (QB/RB/WR/TE)"},
         "partner_team": {"type": "string", "description": "Only trades with this team (team or manager name)"}}}},
    {"name": "get_waiver_targets",
     "description": "Who to pick up and who to drop. Returns your team's weakest positions and upcoming bye/injury holes; the best pickups for your team (lineup points added this week, rest of season and in the playoffs, weeks he'd start, who he replaces, rising snap share, teammate injuries opening up work, trending adds, FAAB bid range); this week's streamers; stash candidates; and your drop list judged long-term (injury length, IR slots, trade value).",
     "parameters": {"type": "object", "properties": {"position": {"type": "string"}}}},
    {"name": "get_defense_vs_position",
     "description": "How many fantasy points (this league's scoring) every NFL defense allows per game to a position. Rank 1 = allows the most = best matchup.",
     "parameters": {"type": "object", "properties": {"position": {"type": "string", "enum": ["QB", "RB", "WR", "TE", "K", "DEF"]}},
                    "required": ["position"]}},
    {"name": "get_weekly_matchup",
     "description": "This week's head-to-head: your projected lineup vs your opponent's, with win probability.",
     "parameters": {"type": "object", "properties": {"week": {"type": "integer"}}}},
    {"name": "get_league_standings",
     "description": "Standings, points for/against, and a power ranking by rest-of-season lineup strength.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_backtest",
     "description": "Huddle's report card for this season: replays every finished week using only what was known before kickoff. How far off its projections were (vs Sleeper alone and season averages), how often it picked the better of two players, the best Sleeper/Huddle mix, and what Huddle's lineups would have scored for the manager each week vs what they actually started (including results that would have flipped).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_team_roster",
     "description": "Another team's roster with projections and values (useful for finding trade targets).",
     "parameters": {"type": "object", "properties": {"team": {"type": "string", "description": "Team name or manager name"}},
                    "required": ["team"]}},
]

# Extra tools offered when an outside AI (Claude Code, Codex, the Claude app) drives Huddle
CONNECTOR_TOOLS: List[Dict[str, Any]] = [
    {"name": "get_league_context",
     "description": "Call this first: the league's format and scoring, the manager's team and record, the current "
                    "NFL week, fantasy playoff weeks, and the manager's standing preferences for advice.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "refresh_data",
     "description": "Re-download rosters, injuries, stats and projections (use if the user says something changed, "
                    "like a trade or waiver claim that just went through).",
     "parameters": {"type": "object", "properties": {}}},
]

CONNECTOR_INSTRUCTIONS = (
    "Huddle is the user's own fantasy football assistant for their Sleeper league, running on their Mac. "
    "At the start of a fantasy conversation, call get_league_context: it returns the league format, the user's "
    "team, the current week and the manager's standing preferences, which you should follow. "
    "Never answer from memory about players, rosters, injuries or matchups - your knowledge is out of date. "
    "Call get_player_report for every player you give advice about and analyze_trade for every trade. "
    "All points are in the league's own scoring. Be decisive and concise."
)

TOOL_LABELS = {
    "get_my_team": "Reading your roster", "get_player_report": "Researching {player}",
    "compare_players": "Comparing {players}", "get_lineup_advice": "Optimizing your lineup",
    "analyze_trade": "Running the trade through both lineups", "find_trades": "Searching the league for trades",
    "get_waiver_targets": "Scanning the waiver wire", "get_defense_vs_position": "Pulling defense-vs-{position} numbers",
    "get_weekly_matchup": "Projecting this week's matchup", "get_league_standings": "Checking the standings",
    "get_team_roster": "Looking at {team}'s roster", "get_league_context": "Loading your league",
    "get_backtest": "Replaying the season so far",
    "refresh_data": "Refreshing the data",
}


def tool_label(name: str, args: Dict[str, Any]) -> str:
    name = (name or "").replace("mcp__huddle__", "")
    tmpl = TOOL_LABELS.get(name, name.replace("_", " ").capitalize())
    vals = {k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in (args or {}).items()}
    try:
        return tmpl.format(**{"player": "a player", "players": "players", "position": "position", "team": "a team", **vals})
    except Exception:
        return tmpl
