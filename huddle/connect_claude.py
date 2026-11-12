"""Add Huddle to the Claude desktop app's connectors.

    ./run.sh --connect-claude        (or: .venv/bin/python -m huddle.connect_claude)

Edits ~/Library/Application Support/Claude/claude_desktop_config.json, keeping
everything already in it and saving a backup first.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

from .config import ROOT


def config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry() -> Dict[str, Any]:
    return {"command": sys.executable, "args": ["-m", "huddle.mcp_server"], "env": {"PYTHONPATH": str(ROOT)}}


def install() -> Dict[str, Any]:
    """Returns {"ok": bool, "message": str, "path": str}."""
    path = config_path()
    data: Dict[str, Any] = {}
    if path.exists() and path.read_text().strip():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {"ok": False, "path": str(path),
                    "message": f"Your Claude config at {path} isn't valid JSON, so Huddle left it alone. "
                               "Fix or delete that file, then try again."}
        backup = path.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, backup)
    servers = data.setdefault("mcpServers", {})
    already = servers.get("huddle") == server_entry()
    servers["huddle"] = server_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    msg = ("Huddle was already connected." if already else "Huddle is now a connector in the Claude desktop app.")
    msg += (" Quit the Claude app completely (Cmd+Q) and open it again, then ask it something like "
            "\"Use Huddle: who should I start this week?\"")
    return {"ok": True, "path": str(path), "message": msg}


def main() -> None:
    res = install()
    print(("\n  " if res["ok"] else "\n  Problem: ") + res["message"] + f"\n  Config: {res['path']}\n")
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
