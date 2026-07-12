#!/usr/bin/env python3
"""
_secrets.py - tiny local secret loader for hopandhaul.

Resolution order for any key:
  1. environment variable  (e.g. DUFFEL_API_KEY)      - wins, so CI/agents can override
  2. secrets.local.json    (this directory, gitignored) - convenient persistent storage

No secret is ever hardcoded in tracked source. secrets.local.json is listed in .gitignore.
Pure stdlib. Import and call get("DUFFEL_API_KEY").
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(HERE, "secrets.local.json")
_cache = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (FileNotFoundError, ValueError):
            _cache = {}
    return _cache


def get(name: str, default=None):
    """Env var first, then secrets.local.json, then default."""
    v = os.environ.get(name)
    if v:
        return v
    v = _load().get(name)
    return v if v else default


def has(name: str) -> bool:
    return bool(get(name))


def mask(name: str) -> str:
    """Safe-to-print fingerprint of a key: 'set (...OovM)' or 'MISSING'."""
    v = get(name)
    if not v:
        return "MISSING"
    tail = v[-4:] if len(v) >= 4 else "****"
    return f"set (…{tail})"
