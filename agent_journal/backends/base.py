"""Backend abstraction for agent-journal.

A Backend hides the differences between LLM providers (Kimi/Moonshot,
MiniMax via Anthropic-compat, Anthropic Claude, OpenAI-compat, etc.)
behind a single `call_llm()` method. The orchestrator (writer.py,
tasks.py) never imports a specific provider — it just calls the
configured Backend.

To add a new provider, drop a new module in this package implementing
the Backend interface and register it in __init__.py BACKENDS.
"""
from typing import Tuple


class Backend:
    """Abstract base. Concrete subclasses override `configure` + `call_llm`."""

    #: short name used in config.json `backend` field.
    name: str = ""

    def configure(self, config: dict, secrets: dict) -> None:
        """Initialize from the per-bot config dict + the resolved secrets dict.

        Implementations should pull pricing, model name, api key, etc. from
        these dicts. Raise on missing required fields so misconfiguration
        surfaces immediately at startup.
        """
        raise NotImplementedError

    def call_llm(self, prompt: str, max_tokens: int = 12000) -> Tuple[str, dict, float]:
        """Send `prompt` to the configured model.

        Returns (text, usage, cost_usd):
          - text:    the assistant's response, no model-specific wrappers
          - usage:   {input_tokens, output_tokens, ...} as a dict
          - cost_usd: dollars, computed from the configured pricing
        """
        raise NotImplementedError

    @property
    def model(self) -> str:
        """Model identifier for logging."""
        return getattr(self, "_model", "")

    @property
    def tier(self) -> str:
        """Logical tier ('L1'/'L2'/'L3') for cost-attribution logs."""
        return getattr(self, "_tier", "L2")
