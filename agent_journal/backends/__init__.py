"""Backend registry. Add a new entry here to register a new provider."""
from .base import Backend
from .kimi import KimiBackend
from .minimax import MiniMaxBackend
from .claude import ClaudeBackend


BACKENDS = {
    "kimi": KimiBackend,
    "minimax": MiniMaxBackend,
    "claude": ClaudeBackend,
}


def load_backend(config: dict, secrets: dict) -> Backend:
    """Instantiate and configure the backend named in `config['backend']`."""
    name = (config.get("backend") or "").lower()
    if name not in BACKENDS:
        raise ValueError(
            f"unknown backend {name!r}; available: {sorted(BACKENDS)}"
        )
    b = BACKENDS[name]()
    b.configure(config, secrets)
    return b
