"""agent-journal: a backend-agnostic daily-journal runner for AI agents.

Sub-modules:
  - writer:   daily journal orchestrator (the morning run)
  - tasks:    cron-driven task runner (self-delegated work)
  - site:     static-site generator (markdown → HTML + RSS)
  - backends: LLM provider abstraction (kimi / minimax / claude / …)

Each module reads a JSON config (per-bot) and a JSON secrets dict, then
runs without depending on any specific agent framework. See INSTALL.md
for the per-bot setup that wires cron, wrappers, and the static site.
"""

__version__ = "0.1.0"
