"""Redact common credentials before logs leave a Launcher."""
from __future__ import annotations

import re

KEY = re.compile(r"(token|secret|password|passwd|authorization|api[_-]?key|bearer|cookie|session)", re.I)
VALUE = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,}|gho_[a-z0-9]{8,}|"
                   r"bearer\s+[a-z0-9._~-]{8,}|[a-z0-9_-]{32,})")


def _string(value):
    return VALUE.sub("[REDACTED]", str(value))


def redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if KEY.search(str(key)):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _string(value)
    return value


def redact_jsonl(text):
    import json
    output = []
    for line in str(text).splitlines():
        try:
            output.append(json.dumps(redact(json.loads(line)), ensure_ascii=False))
        except (TypeError, ValueError):
            output.append(_string(line))
    return "\n".join(output) + ("\n" if output else "")
