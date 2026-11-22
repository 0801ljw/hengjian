"""Operator-configured model prices for Issue cost estimates.

No default prices are bundled: a model without an entry is explicitly
`unpriced`. The estimate uses prompt/completion tokens when the provider
reports them; otherwise it stays unpriced rather than guessing.
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import PolicyError

MAX_RATE = 1_000_000.0


def load(instance):
    path = Path(instance) / "pricing.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"models": {}}
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return {"models": {}}
    clean = {}
    for model, rates in models.items():
        if not isinstance(model, str) or not model or len(model) > 200 or not isinstance(rates, dict):
            continue
        try:
            prompt = float(rates["input_per_million_usd"])
            completion = float(rates["output_per_million_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= prompt <= MAX_RATE and 0 <= completion <= MAX_RATE):
            continue
        clean[model] = {"input_per_million_usd": prompt,
                        "output_per_million_usd": completion}
    return {"models": clean}


def save(instance, data):
    raw = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise PolicyError("pricing.models must be an object")
    clean = {}
    for model, rates in raw.items():
        if not isinstance(model, str) or not model or len(model) > 200 or not isinstance(rates, dict):
            raise PolicyError("invalid model entry")
        try:
            prompt = float(rates["input_per_million_usd"])
            completion = float(rates["output_per_million_usd"])
        except (KeyError, TypeError, ValueError):
            raise PolicyError("each model needs numeric input/output rates") from None
        if not (0 <= prompt <= MAX_RATE and 0 <= completion <= MAX_RATE):
            raise PolicyError("model rates are out of range")
        clean[model] = {"input_per_million_usd": prompt,
                        "output_per_million_usd": completion}
    path = Path(instance) / "pricing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"models": clean}, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return {"models": clean}


def estimate(instance, model, prompt_tokens, completion_tokens):
    if not isinstance(model, str) or model not in load(instance)["models"]:
        return {"pricing": "unpriced", "cost_usd": None, "model": model}
    if type(prompt_tokens) is not int or type(completion_tokens) is not int:
        return {"pricing": "unpriced", "cost_usd": None, "model": model}
    rates = load(instance)["models"][model]
    cost = (prompt_tokens * rates["input_per_million_usd"]
            + completion_tokens * rates["output_per_million_usd"]) / 1_000_000
    return {"pricing": "configured", "cost_usd": round(cost, 8), "model": model,
            "rates": rates}
