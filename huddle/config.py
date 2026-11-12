"""Settings, stored in data/settings.json and editable from the Settings page."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HUDDLE_DATA_DIR", ROOT / "data"))
SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULT_PREFERENCES = (
    "- I care about winning the league, not just this week: weigh rest-of-season value, "
    "bye-week coverage and the fantasy playoffs.\n"
    "- Research every player every time: opponent positional defense, injuries and practice "
    "status, workload and usage, recent form, home/away, venue and game environment. "
    "Don't lean on generic rankings.\n"
    "- For trades, evaluate the impact on my actual starting lineup and apply a realistic "
    "discount to 2-for-1 packages; never just add player values together.\n"
    "- Protect premium players; prefer consolidating depth into higher-value RB/WR starters."
)

DEFAULTS: Dict[str, Any] = {
    "sleeper_username": "",
    "league_id": "",
    # Ask Huddle runs on: claude (Claude subscription via Claude Code) | chatgpt (ChatGPT
    # subscription via Codex) | ollama (free local model) | api (an API key)
    "ai_provider": "claude",
    "claude_sub_model": "",           # "", "sonnet" or "opus"
    "chatgpt_model": "",              # "" = Codex's default for your plan
    "api_vendor": "anthropic",        # for ai_provider=api: anthropic | openai
    "anthropic_api_key": "",          # ANTHROPIC_API_KEY env var also works
    "claude_model": "claude-sonnet-5-5",
    "openai_api_key": "",             # OPENAI_API_KEY env var also works
    "openai_model": "gpt-6.1-sol",
    "openai_url": "https://api.openai.com/v1",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen3:8b",
    "projection_blend": 0.7,           # weight on Sleeper projections vs Huddle's own model
    "playoff_weight": 1.25,            # how much more fantasy-playoff weeks count in ROS values
    "preferences": DEFAULT_PREFERENCES,
    "theme": "field",                  # field (light) | dark
}


PROVIDERS = ("claude", "chatgpt", "ollama", "api")


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            settings.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:
            pass
    # older versions used auto/openai for API keys
    if settings.get("ai_provider") == "openai":
        settings.update(ai_provider="api", api_vendor="openai")
    elif settings.get("ai_provider") not in PROVIDERS:
        settings["ai_provider"] = "claude"
    if settings.get("theme") not in ("field", "dark"):
        settings["theme"] = "field"
    return settings


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    settings = load_settings()
    for key, value in updates.items():
        if key in DEFAULTS:
            settings[key] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    try:
        os.chmod(SETTINGS_PATH, 0o600)   # may hold an API key
    except OSError:
        pass
    return settings


def anthropic_key(settings: Dict[str, Any]) -> str:
    return (settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def openai_key(settings: Dict[str, Any]) -> str:
    return (settings.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def public_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Settings safe to send to the browser (API keys masked)."""
    out = dict(settings)
    out["anthropic_api_key"] = ""
    out["anthropic_key_set"] = bool(anthropic_key(settings))
    out["anthropic_key_from_env"] = bool(os.environ.get("ANTHROPIC_API_KEY")) and not settings.get("anthropic_api_key")
    out["openai_api_key"] = ""
    out["openai_key_set"] = bool(openai_key(settings))
    out["openai_key_from_env"] = bool(os.environ.get("OPENAI_API_KEY")) and not settings.get("openai_api_key")
    return out
