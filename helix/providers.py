"""Provider-agnostic chat-completion router. Free tiers first.

Every real provider is an OpenAI-compatible endpoint (anthropic uses its
Messages API); Helix picks one from HELIX_PROVIDER. `mock` is the default:
a deterministic offline provider so Helix runs end to end at $0 with no
keys - for demos, tests and CI.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "cheap_model": "gemini-2.0-flash",
        "strong_model": "gemini-2.0-flash",
        "cost_per_1k": 0.0,
        "note": "Google AI Studio free tier (recurring)",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "cheap_model": "llama-3.1-8b-instant",
        "strong_model": "llama-3.3-70b-versatile",
        "cost_per_1k": 0.0,
        "note": "Groq free tier (recurring, rate-limited)",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "cheap_model": "meta-llama/llama-3.1-8b-instruct:free",
        "strong_model": "meta-llama/llama-3.3-70b-instruct:free",
        "cost_per_1k": 0.0,
        "note": "OpenRouter :free models (recurring)",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "cheap_model": "llama3.1-8b",
        "strong_model": "llama-3.3-70b",
        "cost_per_1k": 0.0,
        "note": "free tier (recurring, rate-limited)",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": "",
        "cheap_model": "llama3.1:8b",
        "strong_model": "llama3.1:70b",
        "cost_per_1k": 0.0,
        "note": "local models, no key, fully free",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "cheap_model": "gpt-4o-mini",
        "strong_model": "gpt-4o",
        "cost_per_1k": 0.002,
        "note": "paid",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "cheap_model": "claude-haiku-4-5",
        "strong_model": "claude-sonnet-4-5",
        "cost_per_1k": 0.004,
        "note": "paid (Anthropic Messages API)",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "cheap_model": "deepseek-chat",
        "strong_model": "deepseek-reasoner",
        "cost_per_1k": 0.0004,
        "note": "paid, low cost",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "cheap_model": "mistral-small-latest",
        "strong_model": "mistral-large-latest",
        "cost_per_1k": 0.001,
        "note": "paid, has a free experiment tier",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "key_env": "TOGETHER_API_KEY",
        "cheap_model": "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "strong_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "cost_per_1k": 0.0005,
        "note": "paid",
    },
    "custom": {
        "base_url": os.environ.get("HELIX_API_BASE", ""),
        "key_env": "HELIX_API_KEY",
        "cheap_model": os.environ.get("HELIX_MODEL", ""),
        "strong_model": os.environ.get("HELIX_MODEL", ""),
        "cost_per_1k": 0.0,
        "note": "any OpenAI-compatible endpoint",
    },
    "mock": {
        "base_url": "",
        "key_env": "",
        "cheap_model": "helix-mock",
        "strong_model": "helix-mock",
        "cost_per_1k": 0.0,
        "note": "offline deterministic provider, no key needed",
    },
}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    text: str
    usage: Usage
    model: str
    latency_ms: int


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class ModelRouter:
    """Routes calls to cheap vs strong models and enforces a token budget."""

    provider_name: str = field(default_factory=lambda: os.environ.get("HELIX_PROVIDER", "mock"))
    token_budget: int = 60000
    spent: int = 0

    def __post_init__(self):
        if self.provider_name not in PROVIDERS:
            raise ValueError(f"unknown provider {self.provider_name!r}; choose from {list(PROVIDERS)}")
        self.cfg = dict(PROVIDERS[self.provider_name])
        if os.environ.get("HELIX_MODEL"):
            self.cfg["cheap_model"] = self.cfg["strong_model"] = os.environ["HELIX_MODEL"]
        if os.environ.get("HELIX_MODEL_CHEAP"):
            self.cfg["cheap_model"] = os.environ["HELIX_MODEL_CHEAP"]
        if os.environ.get("HELIX_MODEL_STRONG"):
            self.cfg["strong_model"] = os.environ["HELIX_MODEL_STRONG"]
        if os.environ.get("HELIX_API_BASE"):
            self.cfg["base_url"] = os.environ["HELIX_API_BASE"]
        if os.environ.get("HELIX_COST_PER_1K"):
            self.cfg["cost_per_1k"] = float(os.environ["HELIX_COST_PER_1K"])

    def model_for(self, tier: str) -> str:
        if tier == "strong":
            return self.cfg["strong_model"]
        return self.cfg["cheap_model"]

    def _charge(self, usage: Usage):
        self.spent += usage.total
        if self.spent > self.token_budget:
            raise BudgetExceeded(f"token budget {self.token_budget} exceeded ({self.spent})")

    def cost_usd(self) -> float:
        return round(self.spent / 1000 * self.cfg["cost_per_1k"], 6)

    async def chat(self, messages: list[dict], tier: str = "cheap",
                   max_tokens: int = 2048, json_mode: bool = False) -> ChatResult:
        if self.provider_name == "mock":
            result = _mock_chat(messages, self.model_for(tier))
        else:
            result = await self._http_chat(messages, tier, max_tokens, json_mode)
        self._charge(result.usage)
        return result

    async def _http_chat(self, messages, tier, max_tokens, json_mode) -> ChatResult:
        key = os.environ.get(self.cfg["key_env"], "") if self.cfg["key_env"] else "none"
        if self.cfg["key_env"] and not key:
            raise RuntimeError(f"{self.cfg['key_env']} is not set for provider {self.provider_name}")
        body: dict = {
            "model": self.model_for(tier),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        if self.provider_name == "anthropic":
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            turns = [m for m in messages if m["role"] != "system"]
            abody: dict = {"model": body["model"], "max_tokens": max_tokens,
                           "messages": turns, "temperature": 0.2}
            if system:
                abody["system"] = system
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.cfg['base_url']}/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json=abody,
                )
                resp.raise_for_status()
                data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
            u = data.get("usage") or {}
            return ChatResult(
                text=text,
                usage=Usage(u.get("input_tokens", 0), u.get("output_tokens", 0)),
                model=body["model"],
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        u = data.get("usage") or {}
        return ChatResult(
            text=choice,
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
            model=body["model"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _mock_chat(messages: list[dict], model: str) -> ChatResult:
    """Deterministic offline completion: enough to run the whole engine with no key."""
    t0 = time.monotonic()
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if user.startswith("VERIFY:"):
        text = json.dumps({"pass": True, "reason": "output covers the assigned task"})
    elif user.rstrip().endswith("PLAN_JSON:") or user.lstrip().startswith("You are the Helix planner"):
        text = _mock_plan(user)
    else:
        text = (
            "Result for the assigned subtask.\n\n"
            "Key points:\n"
            "1. Scoped the work to exactly what was asked.\n"
            "2. Produced the concrete deliverable for this node.\n"
            "3. Flagged assumptions for the synthesis step.\n\n"
            f"(task excerpt: {user[:160]})"
        )
    prompt_t = sum(_rough_tokens(m["content"]) for m in messages)
    completion_t = _rough_tokens(text)
    return ChatResult(text=text, usage=Usage(prompt_t, completion_t),
                      model=model, latency_ms=int((time.monotonic() - t0) * 1000))


def _mock_plan(user_prompt: str) -> str:
    plan = {
        "goal": user_prompt[:200],
        "token_budget": 60000,
        "nodes": [
            {"id": "research", "kind": "research", "task": "Gather the facts and constraints for the goal", "depends_on": [], "model_tier": "cheap"},
            {"id": "work_a", "kind": "analysis", "task": "Produce the first workstream using the research", "depends_on": ["research"], "model_tier": "cheap"},
            {"id": "work_b", "kind": "code", "task": "Produce the second workstream using the research", "depends_on": ["research"], "model_tier": "cheap"},
            {"id": "synthesis", "kind": "synthesis", "task": "Combine all workstreams into the final deliverable", "depends_on": ["work_a", "work_b"], "approval": True, "model_tier": "strong"},
        ],
    }
    return json.dumps(plan)
