"""Anthropic Claude backend (direct API, not via Claude Code CLI)."""
import requests
from .base import Backend


class ClaudeBackend(Backend):
    name = "claude"

    def configure(self, config, secrets):
        self._model = config.get("model", "claude-opus-4-7")
        self._tier = config.get("tier", "L3")
        self.api_base = config.get("api_base", "https://api.anthropic.com")
        secret_key = config.get("api_key_secret", "ANTHROPIC_API_KEY")
        self.api_key = secrets.get(secret_key)
        if not self.api_key:
            raise RuntimeError(
                f"ClaudeBackend: secret {secret_key!r} not in secrets dict"
            )
        # Opus 4.7 standard rates as of authoring; override in config if tier changes.
        self.input_per_m = float(config.get("input_price_per_m", 15.0))
        self.output_per_m = float(config.get("output_price_per_m", 75.0))
        self.cache_write_per_m = float(
            config.get("cache_write_price_per_m", 18.75)
        )
        self.cache_read_per_m = float(
            config.get("cache_read_price_per_m", 1.50)
        )
        self.timeout = int(config.get("timeout_seconds", 600))

    def call_llm(self, prompt, max_tokens=12000):
        resp = requests.post(
            f"{self.api_base}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = ""
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
        usage = data.get("usage", {}) or {}
        in_tok = usage.get("input_tokens", 0) or 0
        out_tok = usage.get("output_tokens", 0) or 0
        cache_create = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        cost = (
            in_tok / 1_000_000 * self.input_per_m
            + out_tok / 1_000_000 * self.output_per_m
            + cache_create / 1_000_000 * self.cache_write_per_m
            + cache_read / 1_000_000 * self.cache_read_per_m
        )
        norm = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": cache_create,
            "cache_read_tokens": cache_read,
        }
        return text, norm, cost
