"""Tiny on-disk HTTP cache.

Every remote file is stored under data/cache with a timestamp. If the network
fails we fall back to the last good copy, so the app keeps working offline with
slightly stale data.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

USER_AGENT = "Huddle/1.0 (local fantasy football assistant)"


class FetchError(RuntimeError):
    pass


class HttpCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._locks: dict = {}
        self._glock = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._glock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _path(self, url: str) -> Path:
        return self.root / hashlib.sha1(url.encode()).hexdigest()

    def get_text(self, url: str, ttl: float, allow_404: bool = False) -> Optional[str]:
        """Return the body of `url`, using a cached copy younger than `ttl` seconds."""
        path = self._path(url)
        meta = path.with_suffix(".meta")
        with self._lock_for(url):
            if path.exists() and meta.exists():
                try:
                    fetched = json.loads(meta.read_text()).get("t", 0)
                except Exception:
                    fetched = 0
                if time.time() - fetched < ttl:
                    return path.read_text(encoding="utf-8")
            try:
                resp = self._client.get(url)
                if resp.status_code == 404 and allow_404:
                    return None
                resp.raise_for_status()
                text = resp.text
                tmp = path.with_suffix(f".tmp{os.getpid()}")   # the web app and Claude connector may share the cache
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
                meta.write_text(json.dumps({"t": time.time(), "url": url}))
                return text
            except Exception as exc:  # network down, rate-limited, etc.
                if path.exists():
                    return path.read_text(encoding="utf-8")
                if allow_404:
                    return None
                raise FetchError(f"Could not download {url}: {exc}") from exc

    def get_json(self, url: str, ttl: float, allow_404: bool = False) -> Any:
        text = self.get_text(url, ttl, allow_404=allow_404)
        if text is None:
            return None
        return json.loads(text)

    def clear(self, max_age: float = 0) -> None:
        """Expire everything (or everything older than max_age) so the next read refetches."""
        for meta in self.root.glob("*.meta"):
            try:
                data = json.loads(meta.read_text())
                if time.time() - data.get("t", 0) >= max_age:
                    data["t"] = 0
                    meta.write_text(json.dumps(data))
            except Exception:
                pass
