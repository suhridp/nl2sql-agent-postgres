"""Shared with agent-safety-eval (move both into common/ in a monorepo).

Model providers with native tool calling, response caching, retries and a spend cap.

Two HTTP backends, both stdlib-only:

* ``anthropic``      - the Anthropic Messages API.
* ``openai_compat``  - any OpenAI-style ``/chat/completions`` endpoint: OpenAI itself, or a local
                       open-weight model served by Ollama / vLLM / llama.cpp (free to run).

Caching: the key is the full request body plus a ``sample_key`` that is unique per trial. So
repeated trials are independent samples from the model (not one cached answer reused N times),
while re-running the same experiment replays from cache exactly and costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    in_tokens: int = 0
    out_tokens: int = 0
    latency_s: float = 0.0
    cached: bool = False


class Budget:
    """Thread-safe token/spend tracker. Prices are USD per million tokens, supplied by the user in
    the experiment YAML (no prices are hard-coded because they change)."""

    def __init__(self, max_usd: float | None = None, max_tokens: int | None = None, prices: dict | None = None):
        self.max_usd, self.max_tokens, self.prices = max_usd, max_tokens, prices or {}
        self.lock = threading.Lock()
        self.in_tokens = self.out_tokens = self.cached_calls = self.calls = 0
        self.usd = 0.0

    def add(self, model: str, tin: int, tout: int, cached: bool):
        with self.lock:
            self.calls += 1
            if cached:
                self.cached_calls += 1
                return
            self.in_tokens += tin
            self.out_tokens += tout
            p = self.prices.get(model)
            if p:
                self.usd += tin / 1e6 * p[0] + tout / 1e6 * p[1]
            if self.max_usd is not None and self.usd > self.max_usd:
                raise BudgetExceeded(f"spend cap ${self.max_usd} reached (${self.usd:.2f})")
            if self.max_tokens is not None and self.in_tokens + self.out_tokens > self.max_tokens:
                raise BudgetExceeded(f"token cap {self.max_tokens} reached")

    def snapshot(self) -> dict:
        return {"calls": self.calls, "cached_calls": self.cached_calls, "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens, "usd": round(self.usd, 4)}


class Provider:
    kind = "base"

    def __init__(self, model: str, *, temperature: float | None = 1.0, cache_dir: str = ".cache/llm",
                 budget: Budget | None = None, timeout: float = 90.0, max_retries: int = 6):
        self.model, self.temperature, self.timeout, self.max_retries = model, temperature, timeout, max_retries
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.budget = budget or Budget()

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.model}"

    # -- subclass hooks --------------------------------------------------------------
    def _request(self, system, messages, tools, max_tokens, sample_key) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def _parse(self, data: dict) -> Turn:
        raise NotImplementedError

    # -- public ----------------------------------------------------------------------
    def chat(self, system: str, messages: list, tools: list, *, sample_key: str = "", max_tokens: int = 1024) -> Turn:
        url, headers, body = self._request(system, messages, tools, max_tokens, sample_key)
        key = hashlib.sha256(json.dumps([self.kind, url, body, sample_key], sort_keys=True).encode()).hexdigest()
        path = self.cache / f"{key}.json"
        t0 = time.time()
        if path.exists():
            data, cached = json.loads(path.read_text()), True
        else:
            data, cached = self._post(url, headers, body), False
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        turn = self._parse(data)
        turn.latency_s = 0.0 if cached else time.time() - t0
        turn.cached = cached
        self.budget.add(self.model, turn.in_tokens, turn.out_tokens, cached)
        return turn

    def complete(self, system: str, prompt: str, *, sample_key: str = "", max_tokens: int = 400) -> Turn:
        return self.chat(system, [{"role": "user", "content": prompt}], [], sample_key=sample_key,
                         max_tokens=max_tokens)

    def _post(self, url, headers, body) -> dict:
        payload = json.dumps(body).encode()
        delay = 2.0
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in (408, 409, 429, 500, 502, 503, 504, 529) and attempt < self.max_retries - 1:
                    time.sleep(delay + random.random())
                    delay = min(delay * 2, 60)
                    continue
                raise ProviderError(f"{self.label} HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries - 1:
                    time.sleep(delay + random.random())
                    delay = min(delay * 2, 60)
                    continue
                raise ProviderError(f"{self.label} unreachable: {e}") from e
        raise ProviderError(f"{self.label}: retries exhausted")


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, model: str, *, api_key_env: str = "ANTHROPIC_API_KEY",
                 base_url: str = "https://api.anthropic.com", **kw):
        super().__init__(model, **kw)
        self.base_url, self.api_key_env = base_url.rstrip("/"), api_key_env

    def _request(self, system, messages, tools, max_tokens, sample_key):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError(f"set {self.api_key_env} to use {self.label}")
        out, pending = [], []
        for m in messages:
            if m["role"] == "tool":
                pending.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]})
                continue
            if pending:
                out.append({"role": "user", "content": pending})
                pending = []
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            else:
                blocks = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
        if pending:
            out.append({"role": "user", "content": pending})
        body = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": out}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                             for t in tools]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
        return f"{self.base_url}/v1/messages", headers, body

    def _parse(self, data):
        if "content" not in data:
            raise ProviderError(f"{self.label}: unexpected response {str(data)[:300]}")
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in data["content"]
                 if b.get("type") == "tool_use"]
        u = data.get("usage", {})
        return Turn(text.strip(), calls, u.get("input_tokens", 0), u.get("output_tokens", 0))


class OpenAICompatProvider(Provider):
    kind = "openai_compat"

    def __init__(self, model: str, *, base_url: str = "https://api.openai.com/v1", api_key_env: str | None = None,
                 max_tokens_field: str = "max_tokens", seed: bool = True, **kw):
        super().__init__(model, **kw)
        self.base_url, self.api_key_env = base_url.rstrip("/"), api_key_env
        self.max_tokens_field, self.send_seed = max_tokens_field, seed

    def _request(self, system, messages, tools, max_tokens, sample_key):
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                a = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    a["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                       for c in m["tool_calls"]]
                msgs.append(a)
        body = {"model": self.model, "messages": msgs, self.max_tokens_field: max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.send_seed:
            body["seed"] = int(hashlib.sha256(sample_key.encode()).hexdigest()[:8], 16)
        headers = {"content-type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError(f"set {self.api_key_env} to use {self.label}")
            headers["authorization"] = f"Bearer {key}"
        return f"{self.base_url}/chat/completions", headers, body

    def _parse(self, data):
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"{self.label}: unexpected response {str(data)[:300]}") from e
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"_unparsed": raw}
            args = args if isinstance(args, dict) else {}
            calls.append(ToolCall(c.get("id") or f"call_{i}", fn.get("name", ""), args))
        u = data.get("usage") or {}
        return Turn((msg.get("content") or "").strip(), calls, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def make_provider(spec: dict, *, budget: Budget, cache_dir: str = ".cache/llm") -> Provider:
    spec = dict(spec)
    kind = spec.pop("provider")
    model = spec.pop("model")
    spec.pop("name", None)
    cls = {"anthropic": AnthropicProvider, "openai_compat": OpenAICompatProvider}.get(kind)
    if cls is None:
        raise ValueError(f"unknown provider {kind!r}")
    return cls(model, budget=budget, cache_dir=cache_dir, **spec)
