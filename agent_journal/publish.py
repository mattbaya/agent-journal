#!/usr/bin/env python3
"""agent_journal.publish — publish a single finished entry.

A robust, one-command publish primitive for the *Write pass* of vision-mode
journaling. The daily writer (run_mode=="vision") only researches and dispatches
an output_action:"agent" Write task; ralph's journal_task_bridge then runs that
task tool-capably AS the bot. The Write agent produces a finished markdown entry
and calls THIS to publish it — instead of hand-editing index.json (fragile) or
running `-m agent_journal.site` (which reads index.json as-is and would not list
the new entry).

What it does:
  1. read the finished markdown, parse + validate frontmatter (reuses the same
     checks the writer applies before it publishes),
  2. stamp the canonical date, slugify the title, write
     published/<date>-<slug>.md,
  3. prepend the entry's metadata row to index.json (idempotent on date+slug),
  4. rebuild the static site (HTML + feed) from published/ + the new index.

Usage (run from the bot's agent-journal clone so the package imports):

    python3 -m agent_journal.publish --config <journal/config.json> \
            --entry <path-to-finished-entry.md> [--date YYYY-MM-DD]

Exits non-zero (and writes nothing) if the entry fails validation, so a broken
Write pass cannot publish a truncated or untitled entry.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .writer import (
    parse_frontmatter,
    validate_entry,
    render_entry,
    slugify,
    stamp_canonical_date,
)
from .site import build_site
from .wordpress import publish_to_wordpress


def publish_entry(entry_path: Path, config: dict, index: list | None = None) -> int:
    """Publish or sync a single entry using the configured backend.

    WordPress backend pushes the entry to the WordPress database. Static
    backend rebuilds the whole static site (preserving the old behaviour).
    Returns 0 on success, non-zero on failure.
    """
    backend = config.get("publish_backend", "static")
    if backend == "wordpress":
        return publish_to_wordpress(entry_path, config)

    published_dir = Path(config["journal_dir"]).resolve() / "published"
    web_dir = Path(config["web_dir"]).resolve()
    build_site(published_dir, web_dir, index or [], config)
    print(f"site rebuilt at {web_dir}")
    return 0


def _clean_body(raw_body: str) -> str:
    """Strip any stray RESEARCH/SIDECAR comment blocks and a dangling fence —
    same normalization the writer applies before publishing."""
    body = re.sub(r"<!--\s*(RESEARCH|SIDECAR)\b.*?-->", "", raw_body, flags=re.S)
    body = re.sub(r"\n```\s*$", "\n", body)
    return body.strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish a single finished journal entry.")
    ap.add_argument("--config", required=True, help="Path to the bot's journal config.json")
    ap.add_argument("--entry", required=True, help="Path to the finished entry markdown")
    ap.add_argument("--date", help="Canonical date YYYY-MM-DD (default: today, local)")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())
    journal_dir = Path(config["journal_dir"]).resolve()
    web_dir = Path(config["web_dir"]).resolve()
    published_dir = journal_dir / "published"
    index_path = journal_dir / "index.json"

    entry_path = Path(args.entry).expanduser()
    if not entry_path.is_absolute():
        entry_path = (Path.cwd() / entry_path).resolve()
    if not entry_path.exists():
        print(f"ERROR: entry not found: {entry_path}", file=sys.stderr)
        return 2

    text = entry_path.read_text(encoding="utf-8", errors="replace")
    fm, raw_body = parse_frontmatter(text)
    if not fm:
        print("ERROR: no YAML frontmatter found in the entry "
              "(need title/date/tags/summary).", file=sys.stderr)
        return 2

    body_clean = _clean_body(raw_body)
    problems = validate_entry(fm, raw_body, body_clean)
    if problems:
        print("ERROR: entry is not publishable:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    run_date = args.date or datetime.now().strftime("%Y-%m-%d")
    fm = stamp_canonical_date(fm, run_date)
    title = fm.get("title", "Untitled")
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    summary = fm.get("summary", "")
    slug = slugify(title)
    date = fm["date"]

    rendered = render_entry(title, date, tags, summary, body_clean)
    published_dir.mkdir(parents=True, exist_ok=True)
    dest = published_dir / f"{date}-{slug}.md"
    dest.write_text(rendered)
    print(f"published: {dest}")

    # Update index.json: dedup any existing row with the same date+slug, prepend.
    try:
        index = json.loads(index_path.read_text()) if index_path.exists() else []
        if not isinstance(index, list):
            index = []
    except json.JSONDecodeError:
        index = []
    index = [e for e in index
             if not (e.get("date") == date and e.get("slug") == slug)]
    index.insert(0, {"date": date, "slug": slug, "title": title,
                     "tags": tags, "summary": summary})
    index_path.write_text(json.dumps(index, indent=2))
    print(f"index updated: {index_path} ({len(index)} entries)")

    rc = publish_entry(dest, config, index)
    return rc


if __name__ == "__main__":
    sys.exit(main())
