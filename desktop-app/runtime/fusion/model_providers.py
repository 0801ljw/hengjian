"""Curated model-access providers (data only; no network calls)."""
from __future__ import annotations

GROUPS = (
    {"id": "popular", "title": "热门"},
    {"id": "labs", "title": "更多模型厂商"},
    {"id": "fast", "title": "极速推理"},
    {"id": "routers", "title": "路由与网关"},
    {"id": "custom", "title": "自定义"},
)

PROVIDERS = (
    {"id": "openai", "label": "OpenAI", "group": "popular", "protocol": "openai",
     "base_url": "https://api.openai.com/v1", "key_url": "https://platform.openai.com/api-keys"},
    {"id": "anthropic", "label": "Anthropic", "group": "popular", "protocol": "anthropic",
     "base_url": "https://api.anthropic.com", "key_url": "https://console.anthropic.com/settings/keys"},
    {"id": "google", "label": "Google Gemini", "group": "popular", "protocol": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_url": "https://aistudio.google.com/apikey"},
    {"id": "deepseek", "label": "DeepSeek", "group": "popular", "protocol": "openai",
     "base_url": "https://api.deepseek.com/v1", "key_url": "https://platform.deepseek.com/api_keys"},
    {"id": "openrouter", "label": "OpenRouter", "group": "popular", "protocol": "openai",
     "base_url": "https://openrouter.ai/api/v1", "key_url": "https://openrouter.ai/keys"},
    {"id": "xai", "label": "xAI", "group": "labs", "protocol": "openai",
     "base_url": "https://api.x.ai/v1", "key_url": "https://console.x.ai"},
    {"id": "mistral", "label": "Mistral AI", "group": "labs", "protocol": "openai",
     "base_url": "https://api.mistral.ai/v1", "key_url": "https://console.mistral.ai/api-keys"},
    {"id": "groq", "label": "Groq", "group": "fast", "protocol": "openai",
     "base_url": "https://api.groq.com/openai/v1", "key_url": "https://console.groq.com/keys"},
    {"id": "cerebras", "label": "Cerebras", "group": "fast", "protocol": "openai",
     "base_url": "https://api.cerebras.ai/v1", "key_url": "https://cloud.cerebras.ai"},
    {"id": "together", "label": "Together AI", "group": "fast", "protocol": "openai",
     "base_url": "https://api.together.xyz/v1", "key_url": "https://api.together.ai/settings/api-keys"},
    {"id": "fireworks", "label": "Fireworks AI", "group": "fast", "protocol": "openai",
     "base_url": "https://api.fireworks.ai/inference/v1", "key_url": "https://fireworks.ai/account/api-keys"},
    {"id": "custom", "label": "自定义 OpenAI 兼容", "group": "custom", "protocol": "openai",
     "base_url": "", "key_url": None},
    {"id": "custom-anthropic", "label": "自定义 Anthropic 兼容", "group": "custom", "protocol": "anthropic",
     "base_url": "", "key_url": None},
)


def providers():
    return {"groups": list(GROUPS), "providers": list(PROVIDERS)}


def get(identifier):
    return next((entry for entry in PROVIDERS if entry["id"] == identifier), None)
