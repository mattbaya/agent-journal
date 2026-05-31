"""MiniMax backend (Anthropic-compatible endpoint).

MiniMax exposes their Claude-compatible API at api.minimax.io/anthropic.
Auth is a bearer token (MINIMAX_AUTH_TOKEN in clortho or env).
"""
import requests
from .base import Backend


class MiniMaxBackend(Backend):
    name = "minimax"

    def configure(self, config, secrets):
        self._model = config.get("model", "MiniMax-M2.7")
        self._tier = config.get("tier", "L1")
        self.api_base = config.get(
            "api_base", "https://api.minimax.io/anthropic"
        )
        secret_key = config.get("api_key_secret", "MINIMAX_AUTH_TOKEN")
        self.api_key = secrets.get(secret_key)
        if not self.api_key:
            raise RuntimeError(
                f"MiniMaxBackend: secret {secret_key!r} not in secrets dict"
            )
        # Equal in/out pricing per the public table (cheap).
        self.input_per_m = float(config.get("input_price_per_m", 0.30))
        self.output_per_m = float(config.get("output_price_per_m", 0.30))
        self.timeout = int(config.get("timeout_seconds", 600))

    def call_llm(self, prompt, max_tokens=12000):
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = requests.post(
            f"{self.api_base}/v1/messages",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # MiniMax may return both thinking + text blocks; pick the text one.
        text = ""
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
        usage = data.get("usage", {}) or {}
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cost = (
            in_tok / 1_000_000 * self.input_per_m
            + out_tok / 1_000_000 * self.output_per_m
        )
        norm = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": usage.get(
                "cache_creation_input_tokens", 0
            ) or 0,
            "cache_read_tokens": usage.get(
                "cache_read_input_tokens", 0
            ) or 0,
        }
        return text, norm, cost
