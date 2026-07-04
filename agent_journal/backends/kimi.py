"""Moonshot / Kimi backend (with optional MiniMax fallback).

If ``config["fallback"]`` is present (a MiniMax backend config), any Kimi call
that fails on quota/rate/availability (HTTP 429/402/5xx, or a network error)
transparently retries via MiniMax so the journal keeps running even when the
Moonshot account is empty or suspended.
"""
import sys
import requests
from .base import Backend


class KimiBackend(Backend):
    name = "kimi"

    def configure(self, config, secrets):
        self._model = config.get("model", "kimi-k2.5")
        self._tier = config.get("tier", "L2")
        self.api_url = config.get(
            "api_url", "https://api.moonshot.ai/v1/chat/completions"
        )
        secret_key = config.get("api_key_secret", "MOONSHOT_API_KEY")
        self.api_key = secrets.get(secret_key)
        if not self.api_key:
            raise RuntimeError(
                f"KimiBackend: secret {secret_key!r} not in secrets dict"
            )
        self.input_per_m = float(config.get("input_price_per_m", 0.60))
        self.output_per_m = float(config.get("output_price_per_m", 2.50))
        self.timeout = int(config.get("timeout_seconds", 600))

        # Optional MiniMax fallback for when Kimi is out (empty/suspended account).
        self._fallback = None
        fb = config.get("fallback")
        if fb:
            try:
                from .minimax import MiniMaxBackend
                m = MiniMaxBackend()
                m.configure(fb, secrets)
                self._fallback = m
            except Exception as e:  # never let a bad fallback config break Kimi
                print(f"[kimi backend] fallback disabled: {e}", file=sys.stderr)
                self._fallback = None

    def call_llm(self, prompt, max_tokens=12000):
        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            transient = status in (429, 402, 408, 500, 502, 503, 504) or status is None
            if self._fallback is not None and transient:
                print(
                    f"[kimi backend] Kimi call failed (status={status}); "
                    "falling back to MiniMax", file=sys.stderr
                )
                return self._fallback.call_llm(prompt, max_tokens=max_tokens)
            raise

        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        in_tok = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)
        cost = (
            in_tok / 1_000_000 * self.input_per_m
            + out_tok / 1_000_000 * self.output_per_m
        )
        norm = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        return text, norm, cost
