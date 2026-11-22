"""Agent registry: bundled → 24h cache → optional remote HTTPS catalog.

Behavior-critical fields (installable/integrated/docs) always come from the
bundled file so a stale remote entry cannot make an unsupported agent bindable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

BUNDLED = Path(__file__).with_name("registry.json")
CACHE_TTL = 24 * 60 * 60
MAX_BYTES = 2_000_000


def _bundled():
    try:
        data = json.loads(BUNDLED.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _cache_path(instance):
    return Path(instance) / "agent-registry-cache.json" if instance is not None else None


def _read_cache(path):
    try:
        data = json.loads(path.read_text())
        if time.time() - path.stat().st_mtime > CACHE_TTL or not isinstance(data, list):
            return None
        return data
    except (OSError, ValueError):
        return None


def _remote():
    url = os.environ.get("DUTANG_AGENT_REGISTRY_URL", "").strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return None
        data = json.loads(body)
        return data if isinstance(data, list) else None
    except (OSError, ValueError):
        return None


def load(instance=None):
    bundled = _bundled()
    cache = _cache_path(instance)
    cached = _read_cache(cache) if cache else None
    remote = _remote()
    if remote:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(remote, ensure_ascii=False, indent=2))
        except OSError:
            pass
    entries = remote or cached or bundled
    by_name = {entry.get("id"): entry for entry in bundled if isinstance(entry, dict)}
    result = []
    seen = set()
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("id") or raw.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        base = by_name.get(name, {})
        # Behavior fields are local truth; descriptive fields may be remote.
        result.append({**raw, **{key: base[key] for key in
                                 ("integrated", "installable", "docs", "roles") if key in base}})
    for name, entry in by_name.items():
        if name not in seen:
            result.append(dict(entry))
    source = "remote" if remote else ("cache" if cached else "bundled")
    return result, source
