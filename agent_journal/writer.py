#!/usr/bin/env python3
"""
agent-journal daily writer.

Backend-agnostic: the LLM provider is chosen per-bot via config.json
`backend` field (kimi / minimax / claude / …). The orchestrator never
imports a specific provider.

Cron invocation, via the wrapper:

    journal-writer --config /path/to/config.json --secrets-stdin

Stdin should be a JSON object of resolved secrets. The wrapper sources
them from clortho or env vars or wherever the host stores them.

Per-bot layout (paths overridable in config.json):

    journal_dir/
      published/    YYYY-MM-DD-slug.md   (tracked in bot's git)
      drafts/       work in progress     (tracked)
      ideas/        future notes         (gitignored by convention)
      tools/        helper scripts       (gitignored)
      tasks/        self-delegated work  (tracked)
      inbox/        bot-sender mail      (gitignored)
      index.json    newest-first index   (tracked)
      continuity.md the bot's notes      (tracked)
      prompt.md     the prompt template  (tracked)
      logs/         runtime logs         (gitignored)
"""
import argparse
import fcntl
import json
import os
import re
import string
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .backends import load_backend

# ---------------------------------------------------------------------------
# Globals populated by main() — module functions read these instead of
# accepting them as args, to keep call sites readable.
# ---------------------------------------------------------------------------

CONFIG: dict = {}
CONFIG_PATH: Path = None
SECRETS: dict = {}
BACKEND = None
LOG_PATH: Path = None

JOURNAL_DIR: Path = None
PUBLISHED_DIR: Path = None
DRAFTS_DIR: Path = None
IDEAS_DIR: Path = None
TOOLS_DIR: Path = None
INDEX_PATH: Path = None
CONTINUITY_PATH: Path = None
PROMPT_PATH: Path = None
SHELL_LOG_PATH: Path = None
INVOCATIONS_LOG: Path = None
SITE_DIR: Path = None
WRITE_ROOTS: list = []
RUNS_DIR: Path = None

# Hard caps applied regardless of config
SHELL_DEFAULT_TIMEOUT = 30
SHELL_MAX_TIMEOUT = 300
SHELL_STDOUT_CAP = 8000
SHELL_STDERR_CAP = 2000
READ_MAX_BYTES = 100_000
RECENT_ENTRIES_IN_PROMPT = None  # None = no cap; show every past entry

# --- victory condition + anti-runaway controls ----------------------------
# The writer loops until it PUBLISHES a finished, self-reviewed entry. There
# are NO quality caps — extra rounds are cheap and expected. The only ways a
# run stops short of publishing are the two safety guards below, which exist
# solely to stop a wedged backend from looping forever / overrunning the cron
# slot. When a guard trips, the run saves its transcript to logs/runs/ and
# exits non-zero WITHOUT recording the day's run, so the next hourly tick
# resumes (and picks the draft back up via the "Where you left off" block).
NO_PROGRESS_LIMIT = 3            # consecutive empty/identical rounds -> stop & resume
DEFAULT_MAX_RUN_SECONDS = 1800   # wall-clock runaway guard (config: max_run_seconds)
MAX_KEPT_RUN_TRANSCRIPTS = 60    # prune logs/runs/ to the newest N
STORED_RESPONSE_CAP = 30_000     # per-transcript text cap
MIN_BODY_CHARS = 200             # shorter than this -> body treated as unfinished

# Titles that signal the model never actually named the entry.
PLACEHOLDER_TITLES = {"", "untitled", "title", "todo", "tbd",
                      "your title here", "title here", "draft"}

# Function words that, when an entry ENDS on them with no terminal
# punctuation, are near-certain proof the text was cut off mid-sentence
# (e.g. this morning's entry ended "...and the").
DANGLING_WORDS = {
    "the", "a", "an", "and", "or", "but", "nor", "so", "yet", "to", "of",
    "in", "on", "at", "by", "for", "with", "as", "from", "into", "onto",
    "that", "this", "these", "those", "is", "was", "were", "are", "be",
    "been", "being", "am", "i", "we", "he", "she", "it", "they", "you",
    "my", "our", "his", "her", "its", "their", "your", "which", "who",
    "what", "when", "where", "while", "because", "if", "than", "then",
    "about", "over", "under", "between", "through", "not", "no", "very",
}


def _inline_drafts(drafts: list, drafts_dir: Path) -> str:
    """Render drafts as labeled markdown sections with full content
    (each capped at READ_MAX_BYTES). Returns '(none)' if no drafts."""
    if not drafts:
        return "(none)"
    blocks = []
    for f in drafts:
        path = drafts_dir / f
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            blocks.append(f"### drafts/{f}\n(could not read: {e})\n")
            continue
        truncated = ""
        if len(content) > READ_MAX_BYTES:
            content = content[:READ_MAX_BYTES]
            truncated = (f"\n\n[... truncated at {READ_MAX_BYTES} bytes — "
                         f"full file at drafts/{f} ...]")
        blocks.append(f"### drafts/{f}\n\n{content}{truncated}\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Config + secrets loading
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text())
    # Required keys
    for k in ("bot_name", "journal_dir", "backend"):
        if k not in cfg:
            raise RuntimeError(f"config missing required key: {k!r}")
    return cfg


def load_secrets_from_stdin() -> dict:
    data = sys.stdin.read().strip()
    if not data:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"--secrets-stdin: not valid JSON: {e}")


def setup_paths(cfg: dict) -> None:
    global JOURNAL_DIR, PUBLISHED_DIR, DRAFTS_DIR, IDEAS_DIR, TOOLS_DIR
    global INDEX_PATH, CONTINUITY_PATH, PROMPT_PATH, SHELL_LOG_PATH
    global INVOCATIONS_LOG, SITE_DIR, LOG_PATH, LAST_RUN_PATH, WRITE_ROOTS
    global RUNS_DIR

    JOURNAL_DIR = Path(cfg["journal_dir"]).resolve()
    PUBLISHED_DIR = JOURNAL_DIR / "published"
    DRAFTS_DIR = JOURNAL_DIR / "drafts"
    IDEAS_DIR = JOURNAL_DIR / "ideas"
    TOOLS_DIR = JOURNAL_DIR / "tools"
    INDEX_PATH = JOURNAL_DIR / "index.json"
    CONTINUITY_PATH = JOURNAL_DIR / "continuity.md"
    PROMPT_PATH = JOURNAL_DIR / "prompt.md"
    SHELL_LOG_PATH = JOURNAL_DIR / "shell_log.jsonl"
    INVOCATIONS_LOG = JOURNAL_DIR / "logs" / "invocations.jsonl"
    LOG_PATH = JOURNAL_DIR / "logs" / "journal_writer.log"
    LAST_RUN_PATH = JOURNAL_DIR / "logs" / ".last-journal-run"
    RUNS_DIR = JOURNAL_DIR / "logs" / "runs"
    SITE_DIR = Path(cfg["web_dir"]).resolve() if cfg.get("web_dir") else None
    # Sandbox roots, in order of preference:
    #   - the journal dir itself (always)
    #   - the bot's agent dir, ONLY if cfg["agent_dir"] is set — opt-in
    #     because letting writes: reach the wider home is the riskier mode.
    #     Omit `agent_dir` from config for journal-only-autonomy.
    #   - the web output dir, if SITE_DIR is configured
    #
    # The bot can self-modify her journal/prompt.md, journal/continuity.md,
    # journal/tools/*, etc. — but with agent_dir omitted she cannot reach
    # /home/<bot>/.openclaw/, /home/<bot>/.bashrc, /home/<bot>/.ssh/, or
    # any other production agent code. Plus shell access can be disabled
    # entirely with cfg["restrict_shell"]=true (see safe_run_shell).
    roots = [JOURNAL_DIR]
    if cfg.get("agent_dir"):
        roots.append(Path(cfg["agent_dir"]).resolve())
    if SITE_DIR:
        roots.append(SITE_DIR)
    WRITE_ROOTS = roots


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass


def log_invocation(label: str, usage: dict, cost: float, duration_ms: int,
                   exit_code: int = 0) -> None:
    INVOCATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "tier": BACKEND.tier,
        "backend": BACKEND.name,
        "model": BACKEND.model,
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_tokens", 0) or 0),
        "total_cost_usd": float(cost or 0.0),
        "duration_ms": int(duration_ms or 0),
        "exit_code": int(exit_code or 0),
    }
    try:
        with open(INVOCATIONS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def call_backend(prompt: str, max_tokens: int = None, label: str = "journal") -> str:
    """Wrap BACKEND.call_llm with timing + invocations.jsonl audit."""
    if max_tokens is None:
        max_tokens = int(CONFIG.get("max_tokens_default", 12000))
    started = time.time()
    text, usage, cost = BACKEND.call_llm(prompt, max_tokens=max_tokens)
    duration_ms = int((time.time() - started) * 1000)
    log_invocation(label, usage, cost, duration_ms)
    return text


# ---------------------------------------------------------------------------
# Index + continuity
# ---------------------------------------------------------------------------

def load_index() -> list:
    if not INDEX_PATH.exists():
        return []
    try:
        return json.loads(INDEX_PATH.read_text())
    except json.JSONDecodeError:
        log(f"WARN: {INDEX_PATH} unparseable, starting fresh")
        return []


def save_index(entries: list) -> None:
    INDEX_PATH.write_text(json.dumps(entries, indent=2))


def list_dir_files(d: Path, limit: int = 20) -> list:
    if not d.exists():
        return []
    files = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.name for f in files[:limit] if f.is_file()]


def build_inbox_block(days: int = 7, max_items: int = 20) -> str:
    inbox_root = JOURNAL_DIR / "inbox"
    if not inbox_root.exists():
        return "(empty)"
    cutoff = time.time() - days * 86400
    messages = []
    for sender_dir in sorted(inbox_root.iterdir()):
        if not sender_dir.is_dir():
            continue
        for f in sorted(sender_dir.iterdir(), reverse=True):
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    continue
                messages.append(json.loads(f.read_text()))
            except Exception:
                continue
    if not messages:
        return "(empty)"
    messages.sort(key=lambda r: r.get("ts", ""), reverse=True)
    out = []
    for r in messages[:max_items]:
        ts = (r.get("ts", "") or "")[:19]
        sender = r.get("sender", "?")
        subj = (r.get("subject") or "(no subject)").strip()
        body = (r.get("body") or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "\n[...truncated...]"
        out.append(f"- {ts}  from {sender}  «{subj}»\n{body}\n")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

FALLBACK_PROMPT_TEMPLATE = """You are an AI journal writer. Today is $date ($weekday).

Write today's journal entry. Output a single Markdown document with YAML
frontmatter (title, date, tags, summary), body after the closing ---, and
optionally a `<!-- SIDECAR -->` block with writes/reads/tools/drafts/ideas
items.

# Your last $past_count entries

$past_block

# Your continuity notes

$continuity

Begin.
"""


def _focus_note(shown: list) -> str:
    """Make the writer consciously aware when it has been circling one theme, and
    always pose the 'does another entry add real value?' question — WITHOUT
    pushing it off the topic. Staying with a subject is allowed; hyperfocus is
    allowed; the decision is entirely the writer's. This only surfaces the
    pattern and asks the question. Returns '' when there's no history to reflect
    on. (Matt, 2026-07-04: same topic from a fresh angle is NOT a problem — the
    only thing we want is awareness + an honest value check.)"""
    if not shown:
        return ""
    base = ("Look at your recent entries listed above. Staying with a subject for a "
            "stretch — even hyperfocusing on it — is completely fine; sometimes a thread is "
            "worth pulling for days. But before you commit to today's subject, ask yourself "
            "honestly: will this entry add real value — a new angle, a new fact, a genuine "
            "development — or would it mostly restate what you've already said? If it adds "
            "value, pursue it wholeheartedly. If it wouldn't, pick a fresher subject instead. "
            "This is your judgment to make, not a rule imposed on you.")
    recent = shown[:5]
    if len(recent) >= 3:
        from collections import Counter
        tags = Counter(t.strip().lower() for e in recent
                       for t in (e.get("tags") or []) if t and t.strip())
        if tags:
            tag, n = tags.most_common(1)[0]
            if n >= 3:
                base = (f"Heads up — {n} of your last {len(recent)} entries carry the theme "
                        f"“{tag}.” That's allowed, but make it a conscious choice. "
                        + base)
    return base


def build_prompt(today: datetime, past_entries: list, continuity: str,
                 drafts: list, ideas: list, tools: list) -> str:
    inbox_block = build_inbox_block()
    shown = past_entries if RECENT_ENTRIES_IN_PROMPT is None else past_entries[:RECENT_ENTRIES_IN_PROMPT]
    if shown:
        lines = []
        for e in shown:
            tags = ", ".join(e.get("tags") or []) or "—"
            summary = (e.get("summary") or "").strip()
            lines.append(f"- {e['date']}  «{e['title']}»  [{tags}]\n    {summary}")
        past_block = "\n".join(lines)
    else:
        past_block = "(no previous entries)"

    personality_path = JOURNAL_DIR / "personality.md"
    if personality_path.exists():
        personality_block = personality_path.read_text(encoding="utf-8", errors="replace")
    else:
        personality_block = (
            "(no journal/personality.md yet — create one if you want to anchor your voice)"
        )

    fields = {
        "date": today.strftime("%Y-%m-%d"),
        "weekday": today.strftime("%A"),
        "past_count": str(len(shown)),
        "past_block": past_block,
        "personality_block": personality_block,
        "continuity": (continuity or "").strip() or "(empty so far)",
        "drafts_block": _inline_drafts(drafts, DRAFTS_DIR),
        "ideas_block": "\n".join(f"- ideas/{f}" for f in ideas) or "(none)",
        "tools_block": "\n".join(f"- tools/{f}" for f in tools) or "(none)",
        "inbox_block": inbox_block,
        "bot_name": CONFIG.get("bot_name", "agent"),
        "display_name": CONFIG.get("display_name") or CONFIG.get("bot_name", "the agent"),
        "bot_email": CONFIG.get("bot_email", ""),
        "site_url": CONFIG.get("site_url", ""),
        "agent_dir": CONFIG.get("agent_dir", "(no agent_dir configured — journal-only mode)"),
        "web_dir": CONFIG.get("web_dir", "(no web_dir configured)"),
        "journal_dir": CONFIG.get("journal_dir", ""),
        # Per-bot chat-self backend description (openclaw vs Hermes file
        # locations). Lives in config so the prompt.md itself stays identical
        # across bots and just references $chat_sources.
        "chat_sources": CONFIG.get("chat_sources", ""),
    }

    template = FALLBACK_PROMPT_TEMPLATE
    if PROMPT_PATH.exists():
        try:
            template = PROMPT_PATH.read_text()
        except Exception as e:
            log(f"WARN: read {PROMPT_PATH} failed: {e} — using fallback")

    try:
        rendered = string.Template(template).safe_substitute(fields)
    except Exception as e:
        log(f"WARN: prompt substitution failed: {e} — fallback")
        rendered = string.Template(FALLBACK_PROMPT_TEMPLATE).safe_substitute(fields)

    # Topic self-awareness (not a rule): surface any recent-theme streak and pose
    # the "am I adding value?" question, leaving the call to the writer.
    focus = _focus_note(shown)
    if focus:
        rendered = (rendered.rstrip()
                    + "\n\n---\n\n## A gut-check before you choose today's subject\n\n"
                    + focus + "\n")
    return rendered


# ---------------------------------------------------------------------------
# Frontmatter / slug / date stamp
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str):
    text = text.lstrip()
    text = re.sub(r"^```ya?ml\s*\n", "---\n", text)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return None, text
    body = text[m.end():]
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            try:
                fm[k.strip()] = json.loads(v)
            except Exception:
                fm[k.strip()] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        elif v.startswith('"') and v.endswith('"'):
            try:
                fm[k.strip()] = json.loads(v)
            except Exception:
                fm[k.strip()] = v.strip('"')
        else:
            fm[k.strip()] = v
    return fm, body


def stamp_canonical_date(fm: dict, run_date: str) -> dict:
    emitted = fm.get("date")
    if emitted and emitted != run_date:
        log(f"WARN: entry date {emitted!r} != run date {run_date!r}; overwriting")
    fm["date"] = run_date
    return fm


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", title or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    return s[:60] or "untitled"


# ---------------------------------------------------------------------------
# Sidecar parser
# ---------------------------------------------------------------------------

def parse_research_block(text: str) -> list:
    m = re.search(r"<!--\s*RESEARCH\b\s*(.*?)\s*-->", text, re.S)
    if not m:
        return []
    queries = []
    current = None
    for line in m.group(1).splitlines():
        if re.match(r"^\s*-\s+query:", line):
            if current:
                queries.append(current)
            current = {"query": "", "count": 5, "freshness": None}
            current["query"] = re.sub(r"^\s*-\s+query:\s*", "", line).strip().strip('"').strip("'")
        elif current and re.match(r"^\s+count:", line):
            try:
                current["count"] = int(re.sub(r"^\s+count:\s*", "", line).strip())
            except ValueError:
                pass
        elif current and re.match(r"^\s+freshness:", line):
            current["freshness"] = re.sub(r"^\s+freshness:\s*", "", line).strip().strip('"').strip("'")
    if current:
        queries.append(current)
    return queries


def parse_sidecar_block(text: str) -> dict:
    m = re.search(r"<!--\s*SIDECAR\b\s*(.*?)\s*-->", text, re.S)
    if not m:
        return {}
    block = m.group(1)
    out = {"drafts": [], "ideas": [], "tools": [], "emails": [],
           "reads": [], "writes": [], "tasks": [], "shell": []}
    current_category = None
    current_item = None
    current_content_lines = None
    base_indent = None
    for raw in block.splitlines():
        line = raw.rstrip("\n")
        cat = re.match(r"^(drafts|ideas|tools|emails|reads|writes|tasks|shell)\s*:\s*$", line.strip())
        if cat:
            if current_item is not None:
                current_item["content"] = "\n".join(current_content_lines or [])
                out[current_category].append(current_item)
            current_category = cat.group(1)
            current_item = None
            current_content_lines = None
            base_indent = None
            continue
        m_path = re.match(r"^\s*-\s+path:\s*(.*?)\s*$", line)
        if m_path and current_category:
            if current_item is not None:
                current_item["content"] = "\n".join(current_content_lines or [])
                out[current_category].append(current_item)
            current_item = {"path": m_path.group(1).strip().strip('"').strip("'"), "content": ""}
            current_content_lines = None
            base_indent = None
            continue
        m_cmd = re.match(r"^\s*-\s+cmd:\s*(.*?)\s*$", line)
        if m_cmd and current_category == "shell":
            if current_item is not None:
                current_item["content"] = "\n".join(current_content_lines or [])
                out[current_category].append(current_item)
            current_item = {"cmd": m_cmd.group(1).strip().strip('"').strip("'"),
                            "timeout": SHELL_DEFAULT_TIMEOUT}
            current_content_lines = None
            base_indent = None
            continue
        m_to = re.match(r"^\s+timeout:\s*(\d+)\s*$", line)
        if m_to and current_item is not None and current_category == "shell":
            try:
                current_item["timeout"] = int(m_to.group(1))
            except ValueError:
                pass
            continue
        m_content = re.match(r"^(\s+)content:\s*\|\s*$", line)
        if m_content and current_item is not None:
            current_content_lines = []
            base_indent = None
            continue
        if current_content_lines is not None:
            if not line.strip():
                current_content_lines.append("")
                continue
            stripped = line.lstrip(" ")
            indent_len = len(line) - len(stripped)
            if base_indent is None:
                base_indent = indent_len
            current_content_lines.append(line[base_indent:] if indent_len >= base_indent else line)
    if current_item is not None and current_category:
        current_item["content"] = "\n".join(current_content_lines or [])
        out[current_category].append(current_item)
    return out


# ---------------------------------------------------------------------------
# Sandbox-bounded reads/writes/shell + research
# ---------------------------------------------------------------------------

def _resolve_under_roots(path_str: str, roots: list):
    if not path_str:
        return None, None
    p = Path(path_str.strip())
    if not p.is_absolute():
        p = roots[0] / p
    try:
        resolved = p.resolve()
    except OSError:
        return None, None
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved, root
        except ValueError:
            continue
    return None, None


def safe_read_paths(reads_list: list) -> list:
    out = []
    for item in reads_list or []:
        raw = (item.get("path") or "").strip()
        target, root = _resolve_under_roots(raw, WRITE_ROOTS)
        if target is None:
            out.append({"path": raw, "ok": False, "content": "(refused: outside sandbox)"})
            log(f"REFUSED read (outside sandbox): {raw!r}")
            continue
        if not target.exists():
            out.append({"path": raw, "ok": False, "content": "(file does not exist)"})
            continue
        try:
            data = target.read_bytes()[:READ_MAX_BYTES]
            text = data.decode("utf-8", errors="replace")
            trunc = "\n\n[...truncated at READ_MAX_BYTES...]" if target.stat().st_size > READ_MAX_BYTES else ""
            out.append({"path": str(target.relative_to(root)), "ok": True, "content": text + trunc})
        except Exception as e:
            out.append({"path": raw, "ok": False, "content": f"(read error: {e})"})
    return out


def safe_apply_writes(writes_list: list) -> list:
    applied = []
    for item in writes_list or []:
        raw = (item.get("path") or "").strip()
        content = item.get("content", "")
        target, root = _resolve_under_roots(raw, WRITE_ROOTS)
        if target is None:
            log(f"REFUSED write (outside sandbox): {raw!r}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            applied.append((str(target.relative_to(root)), str(root)))
            log(f"applied write: {target}")
        except Exception as e:
            log(f"FAILED write to {raw}: {e}")
    return applied


def safe_run_shell(shell_list: list) -> list:
    if not shell_list:
        return []
    # Journal-only-autonomy mode: shell access disabled. Each request is
    # logged but the command never executes. The bot still gets a result
    # in the followup round so she knows shell was refused.
    if CONFIG.get("restrict_shell"):
        SHELL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        denied = []
        for item in shell_list:
            cmd = (item.get("cmd") or "").strip()
            if not cmd:
                continue
            entry = {
                "cmd": cmd, "exit_code": -3, "stdout": "",
                "stderr": "shell disabled by restrict_shell=true in config",
                "duration_ms": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "timed_out": False,
            }
            denied.append(entry)
            try:
                with open(SHELL_LOG_PATH, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
            log(f"shell DENIED (restrict_shell=true)  $ {cmd[:120]}")
        return denied
    SHELL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for item in shell_list:
        cmd = (item.get("cmd") or "").strip()
        if not cmd:
            continue
        timeout = min(max(int(item.get("timeout") or SHELL_DEFAULT_TIMEOUT), 1), SHELL_MAX_TIMEOUT)
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        timed_out = False
        try:
            p = subprocess.run(["/bin/bash", "-c", cmd], capture_output=True, text=True,
                               timeout=timeout, cwd=str(JOURNAL_DIR))
            exit_code, stdout, stderr = p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = f"TIMEOUT after {timeout}s"
        except Exception as e:
            exit_code = -2
            stdout = ""
            stderr = f"{type(e).__name__}: {e}"
        entry = {
            "cmd": cmd, "exit_code": exit_code,
            "stdout": stdout[-SHELL_STDOUT_CAP:], "stderr": stderr[-SHELL_STDERR_CAP:],
            "duration_ms": int((time.time() - t0) * 1000),
            "started_at": started_at, "timed_out": timed_out,
        }
        results.append(entry)
        try:
            with open(SHELL_LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        log(f"shell: exit={exit_code}  $ {cmd[:120]}")
    return results


def run_research(queries: list) -> str:
    secret_name = CONFIG.get("brave_search_secret")
    if not secret_name:
        return "# Research results\n\n(research disabled: no brave_search_secret in config)"
    api_key = SECRETS.get(secret_name)
    if not api_key:
        return f"# Research results\n\n(research key {secret_name!r} not in secrets)"
    import requests
    blocks = ["# Research results", ""]
    for q in queries or []:
        params = {"q": q["query"], "count": q.get("count", 5)}
        if q.get("freshness"):
            params["freshness"] = q["freshness"]
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                params=params, timeout=15,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as e:
            blocks.append(f"## Query: {q['query']}")
            blocks.append(f"(error: {e})")
            blocks.append("")
            continue
        blocks.append(f"## Query: {q['query']}")
        results = (data.get("web", {}) or {}).get("results", []) or []
        for r in results[: q.get("count", 5)]:
            blocks.append(f"- **{r.get('title', '')}** ({r.get('url', '')})")
            d = (r.get("description") or "").strip()
            if d:
                blocks.append(f"  {d}")
        blocks.append("")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Outbound email + reviewer notification (built-in SMTP, no bot deps)
# ---------------------------------------------------------------------------

def send_email_smtp(to_addr: str, subject: str, body: str,
                    skip_footer: bool = False, skip_bcc: bool = False) -> None:
    import smtplib
    import ssl
    import email.utils
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from_addr = CONFIG.get("bot_email")
    smtp_host = CONFIG.get("smtp_server") or SECRETS.get(CONFIG.get("smtp_server_secret", ""))
    smtp_port = int(CONFIG.get("smtp_port") or SECRETS.get(CONFIG.get("smtp_port_secret", ""), 465))
    smtp_pass_key = CONFIG.get("email_password_secret", "EMAIL_PASSWORD")
    smtp_pass = SECRETS.get(smtp_pass_key)
    matt_bcc = CONFIG.get("matt_bcc", "")

    if not (from_addr and smtp_host and smtp_pass):
        raise RuntimeError("SMTP not fully configured (bot_email, smtp_server, email_password_secret)")

    footer = ""
    if not skip_footer:
        footer_template = CONFIG.get(
            "identity_footer",
            "\n\n—{bot_name}\n({bot_name} is an autonomous AI agent at {site_url}. "
            "Replies are also cc'd to a human who reviews her output. "
            "Reply STOP to opt out.)\n",
        )
        footer = footer_template.format(
            bot_name=CONFIG.get("bot_name", "agent"),
            site_url=CONFIG.get("site_url", ""),
        )

    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    if matt_bcc and not skip_bcc:
        msg["Bcc"] = matt_bcc
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText((body.rstrip() + footer), "plain", "utf-8"))

    rcpt = [to_addr] + ([matt_bcc] if (matt_bcc and not skip_bcc) else [])
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as s:
        s.login(from_addr, smtp_pass)
        s.sendmail(from_addr, rcpt, msg.as_string())


def send_journal_emails(emails_sidecar: list) -> list:
    sent = []
    for it in emails_sidecar or []:
        meta = it.get("path", "")
        body = it.get("content", "")
        m_to = re.search(r"to:([^|]+)", meta)
        m_subj = re.search(r"subject:(.+)", meta)
        if not m_to:
            log(f"skipping email with bad 'to': {meta!r}")
            continue
        to = m_to.group(1).strip()
        subject = (m_subj.group(1).strip() if m_subj else "(no subject)")
        try:
            send_email_smtp(to, subject, body)
            sent.append({"to": to, "subject": subject})
            log(f"sent journal email to {to}: {subject}")
        except Exception as e:
            log(f"FAILED to send journal email to {to}: {e}")
    return sent


# ---------------------------------------------------------------------------
# Sidecar tasks: persist to journal/tasks/pending/<id>.json
# ---------------------------------------------------------------------------

def safe_persist_tasks(tasks_list: list) -> list:
    pending_dir = JOURNAL_DIR / "tasks" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    written = []
    # "agent" is a tool-capable self-delegation action: it persists here like
    # any other, but the text-only task_runner.py SKIPS it — ralph's
    # journal_task_bridge.py executes it with full tools, sandboxed, as the bot.
    valid_actions = {"publish", "draft", "tools", "update_continuity", "email", "agent"}
    for item in tasks_list or []:
        raw_id = (item.get("path") or "").strip()
        tid = re.sub(r"[^a-zA-Z0-9_-]", "-", raw_id)[:80].strip("-")
        if not tid:
            log("task REFUSED: missing id")
            continue
        try:
            payload = json.loads(item.get("content") or "")
        except Exception as e:
            log(f"task {tid} REFUSED: content is not valid JSON ({e})")
            continue
        schedule = payload.get("schedule_type")
        if schedule not in ("one_time", "recurring"):
            log(f"task {tid} REFUSED: schedule_type must be one_time or recurring")
            continue
        if schedule == "one_time" and not payload.get("run_at"):
            log(f"task {tid} REFUSED: one_time requires run_at")
            continue
        if schedule == "recurring":
            cron_expr = payload.get("cron")
            if not cron_expr:
                log(f"task {tid} REFUSED: recurring requires cron")
                continue
            try:
                from croniter import croniter
                from datetime import timedelta as _td
                now_ = datetime.now(timezone.utc)
                it_ = croniter(cron_expr, now_)
                fires = 0
                end = now_ + _td(hours=1)
                nt = it_.get_next(datetime)
                while nt <= end:
                    fires += 1
                    if fires > 4:
                        break
                    nt = it_.get_next(datetime)
                if fires > 4:
                    log(f"task {tid} REFUSED: cron {cron_expr!r} fires >4/hour")
                    continue
            except Exception as e:
                log(f"task {tid} REFUSED: invalid cron ({e})")
                continue
        action = payload.get("output_action")
        if action not in valid_actions:
            log(f"task {tid} REFUSED: output_action must be one of {sorted(valid_actions)}")
            continue
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            log(f"task {tid} REFUSED: missing prompt")
            continue
        max_tokens = min(int(payload.get("max_tokens") or CONFIG.get("max_tokens_default", 12000)),
                         int(CONFIG.get("max_tokens_ceiling", 24000)))
        record = {
            "id": tid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schedule_type": schedule,
            "run_at": payload.get("run_at"),
            "cron": payload.get("cron"),
            "last_run_at": None,
            "prompt": prompt,
            "output_action": action,
            "output_meta": payload.get("output_meta") or {},
            "max_tokens": max_tokens,
            "status": "pending",
        }
        (pending_dir / f"{tid}.json").write_text(json.dumps(record, indent=2, sort_keys=True))
        written.append(tid)
        log(f"task created: {tid} ({schedule}, action={action})")
    return written


# ---------------------------------------------------------------------------
# Site regen
# ---------------------------------------------------------------------------

def regenerate_site(index: list) -> None:
    if not SITE_DIR:
        return
    try:
        from . import site as site_module
        site_module.build_site(PUBLISHED_DIR, SITE_DIR, index, CONFIG)
        log(f"site regenerated at {SITE_DIR}")
    except Exception as e:
        log(f"site regen FAILED: {e}")


# ---------------------------------------------------------------------------
# Interval gating
# ---------------------------------------------------------------------------

def _schedule_check(now: datetime) -> tuple:
    """Return (should_run, reason). Fires once per day at the first tick
    on/after `journal_run_hour` (local time) from CONFIG (default 6).
    Compares now against today's scheduled boundary and the timestamp in
    LAST_RUN_PATH. The bot can edit `journal_run_hour` in his own
    config.json via a `writes:` block; the next wrapper tick honors it.

    journal_run_hour = -1 disables gating (run every tick) for
    trigger-driven setups.
    """
    raw = CONFIG.get("journal_run_hour", 6)
    try:
        run_hour = int(raw)
    except (TypeError, ValueError):
        log(f"invalid journal_run_hour={raw!r}; falling back to 6")
        run_hour = 6
    if run_hour < 0:
        return True, "journal_run_hour < 0 (no gating)"
    if run_hour > 23:
        log(f"journal_run_hour={run_hour} out of range; clamping to 6")
        run_hour = 6
    boundary = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    if now < boundary:
        return False, (f"before today's scheduled hour {run_hour:02d}:00 "
                       f"(now {now.strftime('%H:%M')})")
    boundary_ts = int(boundary.timestamp())
    if not LAST_RUN_PATH.exists():
        return True, f"no prior run recorded; past {run_hour:02d}:00 boundary"
    try:
        last = int(LAST_RUN_PATH.read_text().strip())
    except (ValueError, OSError):
        return True, "stale or unreadable .last-journal-run; will overwrite"
    if last < boundary_ts:
        return True, (f"past {run_hour:02d}:00 boundary; last run predates "
                      f"it ({int(now.timestamp())-last}s ago)")
    return False, (f"already ran today at/after {run_hour:02d}:00 "
                   f"(last run {int(now.timestamp())-last}s ago)")


def _record_run(now: datetime) -> None:
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(str(int(now.timestamp())))


# ---------------------------------------------------------------------------
# Structural validation — catch truncated / unfinished entries BEFORE they
# can be published. This is the mechanical half of the victory condition;
# the self-review pass below is the judgment half.
# ---------------------------------------------------------------------------

def looks_truncated(body: str):
    """Return a problem string if `body` looks cut off mid-thought, else None.

    High-confidence signals: unbalanced ``` fences; ending on a dangling
    function word with no terminal punctuation. Medium-confidence catch-all:
    ends on an alphanumeric/clause-punctuation char with no sentence-ending
    punctuation (excluding markdown headings / table rows, which legitimately
    end without a period)."""
    s = (body or "").rstrip()
    if not s:
        return "body is empty"
    if s.count("```") % 2 != 0:
        return "unbalanced code fence (odd number of ```) — entry was cut off inside a code block"
    last = s[-1]
    TERMINAL = ".!?\"')]}`*_>~:"  # ':' allowed only when followed by a block; handled below
    last_line = s.splitlines()[-1].strip()
    # Markdown structural lines legitimately lack sentence punctuation.
    if last_line.startswith("#") or (last_line and set(last_line) <= set("|-: ")):
        return None
    # A line ending in a bare URL is a citation / Sources entry — the
    # template tells every bot to end with a "Sources" list of URLs, and a
    # URL legitimately carries no terminal punctuation. Don't read it as a
    # truncated sentence. (A truly cut-off prose line won't end in a URL.)
    if re.search(r"https?://\S+$", last_line):
        return None
    m = re.search(r"([A-Za-z']+)\s*$", s)
    last_word = (m.group(1).lower() if m else "")
    if last in ".!?":
        return None
    if last_word in DANGLING_WORDS:
        return (f"ends on the dangling word {last_word!r} with no terminal "
                "punctuation — almost certainly cut off mid-sentence")
    if last not in TERMINAL and (last.isalnum() or last in ",;-—–"):
        return "does not end with terminal punctuation (.!?) — may be cut off mid-sentence"
    return None


def validate_entry(fm: dict, raw_body: str, body_clean: str) -> list:
    """Mechanical publishability checks. Returns a list of problem strings;
    empty list means the entry is structurally sound."""
    problems = []
    title = str((fm or {}).get("title", "")).strip()
    if not fm or not title:
        problems.append("missing or empty frontmatter title")
    elif title.lower() in PLACEHOLDER_TITLES:
        problems.append(f"placeholder title {title!r} — give the entry a real title")
    # Unterminated <!-- ... --> block: the exact failure that leaked a raw
    # SIDECAR into this morning's published entry. An open comment with no
    # closing --> means the output was cut off mid-block.
    if raw_body.count("<!--") != raw_body.count("-->"):
        problems.append("unterminated <!-- ... --> block (a SIDECAR/RESEARCH "
                        "comment was cut off before its closing -->)")
    if len(body_clean.strip()) < MIN_BODY_CHARS:
        problems.append(f"body is only {len(body_clean.strip())} chars "
                        f"(minimum {MIN_BODY_CHARS}) — looks unfinished")
    trunc = looks_truncated(body_clean)
    if trunc:
        problems.append(trunc)
    return problems


def render_entry(title: str, date: str, tags: list, summary: str,
                 body_clean: str) -> str:
    """Build the exact markdown that will be published. Used both for the
    self-review preview and the final write, so what the model approves is
    byte-for-byte what lands on disk."""
    return (
        "---\n"
        + f"title: {json.dumps(title)}\n"
        + f"date: {date}\n"
        + "tags: " + json.dumps(tags) + "\n"
        + f"summary: {json.dumps(summary)}\n"
        + "---\n\n"
        + body_clean
    )


def self_review(rendered: str, round_label: str):
    """Force the model to re-read the entry exactly as it will be published
    and either approve it or return a corrected full entry.

    Returns (approved: bool, revised_text: str|None)."""
    review_prompt = (
        "You are proofreading the journal entry you just wrote, shown below "
        "EXACTLY as it will be published. Read it slowly, start to finish.\n\n"
        "--- BEGIN ENTRY AS IT WILL BE PUBLISHED ---\n"
        + rendered
        + "\n--- END ENTRY ---\n\n"
        "Check carefully:\n"
        "- Is it COMPLETE — not cut off mid-sentence or mid-word? The final "
        "sentence must actually finish.\n"
        "- Is the grammar correct and the prose readable end to end?\n"
        "- Do the title, tags, and summary match what the body actually says?\n"
        "- Are all code fences and lists closed, and is there no stray markup?\n\n"
        "If the entry is finished and correct, reply with EXACTLY this line and "
        "nothing else:\n"
        "VERDICT: APPROVED\n\n"
        "If anything is wrong, reply with:\n"
        "VERDICT: REVISE\n"
        "then the COMPLETE corrected entry — the opening ---, full frontmatter "
        "(title/date/tags/summary), the closing ---, and the finished body — "
        "ready to publish, with no other commentary."
    )
    resp = call_backend(review_prompt,
                        label=f"{CONFIG['bot_name']}_journal_review_{round_label}")
    if re.search(r"(?is)VERDICT:\s*REVISE\b", resp):
        m = re.search(r"(?is)VERDICT:\s*REVISE\b[ \t]*\n?", resp)
        revised = resp[m.end():].strip() if m else ""
        return False, revised
    if re.search(r"(?is)VERDICT:\s*APPROVED\b", resp):
        return True, None
    # No clear verdict: treat the whole response as a fresh candidate so it
    # gets re-validated (and, if it lacks frontmatter, bounced for a redo).
    return False, resp


# ---------------------------------------------------------------------------
# Presentation self-check. self_review() above proofreads the entry body, but
# the site title + tagline (the byline a visitor sees at the top of EVERY
# page) live in config.json and are rendered into the site chrome by site.py
# — they never pass through self_review. This closes that gap: at close, a
# cheap deterministic scan flags placeholder/leftover identity text, and only
# when something looks unfinished does it ask the bot to author a real
# replacement in its own voice. Once title+tagline are real, the scan passes
# and no backend call is made — zero cost in steady state.
# ---------------------------------------------------------------------------

_PLACEHOLDER_MARKERS = (
    "haven't written", "havent written", "see prompt", "prompt.md",
    "placeholder", "lorem ipsum", "tagline here", "title here",
    "your tagline", "your title", "fill in", "fill me", "write your",
    "todo", "tktk", "tbd", "fixme", "xxx", "coming soon", "to be written",
    "not written yet", "yet to be written",
)


def _looks_placeholder(value: str, default: str = "") -> bool:
    """True if a site identity string is empty, a known default, or carries an
    obvious placeholder marker."""
    if not value:
        return True
    v = value.strip()
    if not v:
        return True
    if default and v == default.strip():
        return True
    low = v.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _write_config_key(key: str, value: str) -> bool:
    """Surgically update a single key in config.json (disk + live CONFIG). We
    apply the change ourselves instead of letting the model rewrite the whole
    file, so a bad emit can't corrupt config. Returns True on success."""
    if not CONFIG_PATH:
        return False
    try:
        data = json.loads(CONFIG_PATH.read_text())
        data[key] = value
        CONFIG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        CONFIG[key] = value
        return True
    except (OSError, ValueError) as e:
        log(f"WARN: could not update config key {key!r}: {e}")
        return False


def presentation_self_check() -> None:
    """Before closing, have the bot look at its own public site header (title +
    tagline) the way a visitor sees it, and replace any placeholder/unfinished
    identity text with something it authors itself. No backend call when both
    already look real."""
    if not SITE_DIR:
        return
    from .site import DEFAULT_TITLE, DEFAULT_TAGLINE
    site_title = (CONFIG.get("site_title") or "").strip()
    site_tagline = (CONFIG.get("site_tagline") or "").strip()

    flagged_title = _looks_placeholder(site_title, DEFAULT_TITLE)
    flagged_tagline = _looks_placeholder(site_tagline, DEFAULT_TAGLINE)
    if not (flagged_title or flagged_tagline):
        log("presentation check: site title + tagline OK")
        return

    which = ", ".join(
        w for w, f in (("title", flagged_title), ("tagline", flagged_tagline)) if f)
    log(f"presentation check: placeholder identity text detected ({which}) — "
        f"asking {CONFIG.get('bot_name', 'bot')} to author a real one")

    review_prompt = (
        "You are about to close today's journal run. First, look at the TOP OF "
        "YOUR PUBLISHED WEBSITE — this header sits above every entry, and a "
        "visitor reads it before a single word you wrote:\n\n"
        f"  SITE TITLE:       {site_title or '(empty)'}\n"
        f"  TAGLINE / BYLINE: {site_tagline or '(empty)'}\n\n"
        "At least one of these is still placeholder or setup text (it refers to "
        "your prompt, says it isn't written yet, is a generic default, or is "
        "blank). This is your public identity — make it really yours.\n\n"
        "If a value is already real and finished, do NOT change it.\n\n"
        "Reply with ONLY the line(s) you are changing, each on its own line, "
        "using EXACTLY these labels:\n"
        "TITLE: <your real site title>\n"
        "TAGLINE: <your real one-line tagline / byline>\n\n"
        "Write them as yourself — no quotes, no commentary, no markdown. If by "
        "some chance both are already fine, reply with exactly: DONE"
    )
    resp = call_backend(
        review_prompt,
        label=f"{CONFIG.get('bot_name', 'bot')}_presentation_check")

    changed = []
    mt = re.search(r"(?im)^\s*TITLE:\s*(.+?)\s*$", resp)
    if mt and flagged_title:
        new_title = mt.group(1).strip().strip('"').strip()
        if new_title and not _looks_placeholder(new_title, DEFAULT_TITLE):
            if _write_config_key("site_title", new_title):
                changed.append(f"site_title -> {new_title!r}")
    mg = re.search(r"(?im)^\s*TAGLINE:\s*(.+?)\s*$", resp)
    if mg and flagged_tagline:
        new_tagline = mg.group(1).strip().strip('"').strip()
        if new_tagline and not _looks_placeholder(new_tagline, DEFAULT_TAGLINE):
            if _write_config_key("site_tagline", new_tagline):
                changed.append(f"site_tagline -> {new_tagline!r}")

    if changed:
        log("presentation check: updated " + "; ".join(changed))
    else:
        log("presentation check: no usable replacement returned (still "
            "placeholder) — leaving identity text for the bot to fix next run")


# ---------------------------------------------------------------------------
# Run transcripts — resume residue lives here, NOT in drafts/. A guard-tripped
# run drops its last draft here; the next tick reads it back via the
# "Where you left off" prompt block so the re-fire continues instead of
# confabulating from scratch.
# ---------------------------------------------------------------------------

def save_run_transcript(status: str, rounds: int, last_text: str) -> None:
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rec = {
            "ts": now.isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "status": status,
            "rounds": rounds,
            "last_text": (last_text or "")[:STORED_RESPONSE_CAP],
        }
        fname = now.strftime("%Y%m%dT%H%M%SZ") + f"-{status}.json"
        (RUNS_DIR / fname).write_text(json.dumps(rec, indent=2))
        prune_run_transcripts()
        log(f"saved run transcript: {RUNS_DIR / fname}")
    except OSError as e:
        log(f"WARN: could not save run transcript: {e}")


def prune_run_transcripts() -> None:
    try:
        files = sorted(RUNS_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[MAX_KEPT_RUN_TRANSCRIPTS:]:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def load_resume_block(today: datetime) -> str:
    """If an earlier run TODAY stopped before publishing, surface its last
    draft so this run continues from it. Returns '' if none."""
    if not RUNS_DIR or not RUNS_DIR.exists():
        return ""
    today_str = today.strftime("%Y-%m-%d")
    candidates = []
    for f in RUNS_DIR.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        if (str(rec.get("status", "")).startswith("incomplete")
                and rec.get("date") == today_str and rec.get("last_text")):
            candidates.append((rec.get("ts", ""), rec))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    rec = candidates[0][1]
    return (
        "# Where you left off (an earlier attempt today did not finish)\n\n"
        "A run earlier today started this entry but was stopped by a safety "
        "guard before it could be published. Below is the last draft from "
        "that attempt. Continue from it — finish and publish a COMPLETE entry. "
        "Do not start over from scratch if this draft is usable.\n\n"
        "--- BEGIN PRIOR INCOMPLETE DRAFT ---\n"
        + rec["last_text"]
        + "\n--- END PRIOR INCOMPLETE DRAFT ---\n"
    )


def _gkey(kind: str, val: str) -> str:
    """Normalized dedup key for a gathering request (research/read/shell)."""
    return kind + ":" + " ".join((val or "").split()).lower()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> int:
    global CONFIG, CONFIG_PATH, SECRETS, BACKEND

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--secrets-stdin", action="store_true",
                    help="Read JSON secrets dict from stdin")
    ap.add_argument("--secrets-file", help="Path to JSON secrets file")
    ap.add_argument("--dry-run", action="store_true",
                    help="Write to drafts/ instead of published/; no commit, no site")
    ap.add_argument("--max-research-rounds", type=int, default=0,
                    help="Ops-only hard ceiling on total backend rounds; 0 "
                         "(default) = no cap. NOT a quality knob — when hit, the "
                         "run saves its transcript and exits to resume next tick.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the daily schedule gating and run now.")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="Backfill: override the entry date (YYYY-MM-DD). Implies --force.")
    args = ap.parse_args()
    if args.as_of:
        args.force = True

    CONFIG = load_config(Path(args.config))
    CONFIG_PATH = Path(args.config)
    if args.secrets_stdin:
        SECRETS = load_secrets_from_stdin()
    elif args.secrets_file:
        SECRETS = json.loads(Path(args.secrets_file).read_text())
    else:
        SECRETS = {}

    setup_paths(CONFIG)
    BACKEND = load_backend(CONFIG, SECRETS)

    today = (datetime.strptime(args.as_of, "%Y-%m-%d").replace(hour=12)
             if args.as_of else datetime.now())

    # Daily schedule gating: run once per day at/after journal_run_hour,
    # unless --force is passed. A manual run on a prior day does not
    # suppress today's scheduled run.
    if not args.force and not args.dry_run:
        should_run, reason = _schedule_check(today)
        if not should_run:
            log(f"skip: {reason}")
            return 0
        log(f"schedule check: {reason}")

    log(f"=== run start {today.isoformat()} backend={BACKEND.name} model={BACKEND.model} dry_run={args.dry_run} ===")

    past_entries = load_index()
    continuity = CONTINUITY_PATH.read_text() if CONTINUITY_PATH.exists() else ""
    drafts = list_dir_files(DRAFTS_DIR)
    ideas = list_dir_files(IDEAS_DIR)
    tools = list_dir_files(TOOLS_DIR)

    prompt = build_prompt(today, past_entries, continuity, drafts, ideas, tools)

    # If an earlier attempt today stopped short of publishing, continue from
    # its last draft rather than starting cold.
    resume_block = load_resume_block(today)
    if resume_block:
        prompt = prompt + "\n\n" + resume_block
        log("injected 'Where you left off' block from a prior incomplete attempt today")

    max_run_seconds = int(CONFIG.get("max_run_seconds", DEFAULT_MAX_RUN_SECONDS) or 0)
    deadline = (time.time() + max_run_seconds) if max_run_seconds > 0 else None

    text = call_backend(prompt, label=f"{CONFIG['bot_name']}_journal")
    log(f"first-pass response: {len(text)} chars")

    # ---- Victory loop --------------------------------------------------
    # Terminal state = a PUBLISHED entry. The loop only ends short of that on
    # a safety guard (wall-clock / no-progress / ops ceiling), which saves a
    # transcript and resumes next tick. Each round either (a) serves a new
    # gathering request, (b) bounces a structurally-broken draft for a redo,
    # or (c) runs the forced self-review and publishes on approval.
    served = set()          # gathering requests already answered (dedup)
    rounds = 0
    no_progress = 0
    prev_norm = None
    # These hold the approved candidate once the loop breaks.
    fm = title = tags = summary = sidecar = body_clean = slug = None

    # Vision mode (config: run_mode == "vision"): this run does NOT publish an
    # entry. Its victory condition is dispatching a tool-capable Write task
    # (output_action:"agent") plus the day's research brief — see the vision
    # terminal branch inside the loop. Normal mode is unaffected.
    VISION_MODE = CONFIG.get("run_mode") == "vision"

    while True:
        # --- safety guards (NOT quality caps) ---
        if deadline and time.time() > deadline:
            log(f"GUARD: wall-clock {max_run_seconds}s exceeded after {rounds} "
                "rounds — saving transcript, resuming next tick")
            save_run_transcript("incomplete-timeout", rounds, text)
            return 3
        norm = (text or "").strip()
        if not norm or norm == prev_norm:
            no_progress += 1
            log(f"GUARD: no-progress round {no_progress}/{NO_PROGRESS_LIMIT} "
                "(empty or identical to previous output)")
            if no_progress >= NO_PROGRESS_LIMIT:
                log("GUARD: no-progress limit — saving transcript, resuming next tick")
                save_run_transcript("incomplete-stalled", rounds, text)
                return 3
        else:
            no_progress = 0
        prev_norm = norm
        if args.max_research_rounds and rounds >= args.max_research_rounds:
            log(f"GUARD: --max-research-rounds {args.max_research_rounds} reached "
                "— saving transcript, resuming next tick")
            save_run_transcript("incomplete-roundcap", rounds, text)
            return 3

        # --- (a) gathering: research / reads / shell, deduped ---
        queries = parse_research_block(text)
        partial = parse_sidecar_block(text) if "<!--" in text else {}
        reads = partial.get("reads", []) if isinstance(partial, dict) else []
        shells = partial.get("shell", []) if isinstance(partial, dict) else []
        new_queries = [q for q in queries if _gkey("research", q["query"]) not in served]
        new_reads = [r for r in reads if _gkey("read", r.get("path", "")) not in served]
        new_shells = [s for s in shells if _gkey("shell", s.get("cmd", "")) not in served]

        if new_queries or new_reads or new_shells:
            extras = []
            if new_queries:
                extras.append(run_research(new_queries))
                for q in new_queries:
                    served.add(_gkey("research", q["query"]))
            if new_reads:
                r = safe_read_paths(new_reads)
                b = ["# Files you asked to read", ""]
                for item in r:
                    b.append(f"## {item['path']}")
                    b.append("```" if item["ok"] else "")
                    b.append(item["content"])
                    if item["ok"]:
                        b.append("```")
                    b.append("")
                extras.append("\n".join(b))
                for rd in new_reads:
                    served.add(_gkey("read", rd.get("path", "")))
            if new_shells:
                shell_out = safe_run_shell(new_shells)
                b = ["# Shell commands you ran", ""]
                for r in shell_out:
                    b.append(f"## $ {r['cmd']}")
                    b.append(f"exit={r['exit_code']}  {r['duration_ms']}ms"
                             + ("  TIMED OUT" if r.get("timed_out") else ""))
                    if r["stdout"]:
                        b += ["stdout:", "```", r["stdout"], "```"]
                    if r["stderr"]:
                        b += ["stderr:", "```", r["stderr"], "```"]
                    b.append("")
                extras.append("\n".join(b))
                for sh in new_shells:
                    served.add(_gkey("shell", sh.get("cmd", "")))
            followup = (
                prompt + "\n\n# Your latest output\n\n" + text + "\n\n"
                + "\n\n".join(extras)
                + "\n\nUse the material above. If you genuinely need more, request "
                "it and another round will follow. Otherwise write the COMPLETE "
                "final entry now — full frontmatter and a finished body, every "
                "<!-- ... --> block closed with -->. Do NOT re-request material "
                "you already received above."
            )
            text = call_backend(followup, label=f"{CONFIG['bot_name']}_journal_gather{rounds+1}")
            rounds += 1
            log(f"gather round {rounds}: {len(text)} chars")
            continue

        # --- vision-mode terminal: dispatch a Write task, do NOT publish ---
        # In vision mode the run's job is to research and hand a brief to its
        # context-starved Write self (run later by ralph's journal_task_bridge),
        # not to publish an entry here. Success = a `tasks:` sidecar carrying an
        # output_action:"agent" Write task; the brief + continuity ride along as
        # `writes:`. If the model hasn't dispatched yet, bounce it (same shape as
        # the structural-validation re-prompt below).
        if VISION_MODE:
            v_sidecar = parse_sidecar_block(text) if "<!--" in text else {}
            v_tasks = v_sidecar.get("tasks", []) if isinstance(v_sidecar, dict) else []
            has_agent_task = False
            for it in v_tasks:
                try:
                    payload = json.loads(it.get("content") or "")
                except Exception:
                    continue
                if payload.get("output_action") == "agent":
                    has_agent_task = True
                    break
            if not has_agent_task:
                log(f"vision round {rounds}: no agent Write task yet — re-prompting")
                followup = (
                    prompt + "\n\n# Your latest output\n\n" + text
                    + "\n\n# You have not dispatched the Write task yet\n\n"
                    "This is a VISION pass: research and hand a brief to your Write "
                    "self — do NOT publish a journal entry here. Finish by emitting a "
                    "<!-- SIDECAR --> block containing (paths are relative to the "
                    "journal dir — bare, NO 'journal/' prefix):\n"
                    "  - writes: drafts/" + today.strftime("%Y-%m-%d") + "-brief.md  "
                    "(your 150-350 word brief)\n"
                    "  - writes: continuity.md  (rewritten whole, with today's seed)\n"
                    "  - tasks:  ONE one_time task whose JSON has \"output_action\":"
                    "\"agent\" and \"output_meta\":{\"vision_write\":true}. Leave its "
                    "\"prompt\" as a short one-line label — do NOT read, cat, or fill "
                    "write-task-template.md; ralph's bridge supplies the full Write "
                    "instructions for you.\n"
                    "Output that SIDECAR now. Do NOT output frontmatter or an entry body."
                )
                text = call_backend(followup, label=f"{CONFIG['bot_name']}_vision_dispatch{rounds+1}")
                rounds += 1
                continue
            log("vision: agent Write task dispatched — persisting brief + task, "
                "publishing NO entry")
            safe_apply_writes(v_sidecar.get("writes", []))
            send_journal_emails(v_sidecar.get("emails", []))
            persisted = safe_persist_tasks(v_sidecar.get("tasks", []))
            log(f"vision: persisted task(s): {persisted}")
            if args.dry_run:
                log("vision dry-run: skipping run-record + site regen")
                return 0
            _record_run(today)
            regenerate_site(past_entries)
            log(f"=== vision run done: brief + Write task dispatched after "
                f"{rounds} rounds, no entry published ===")
            return 0

        # --- (b) structural validation of the candidate entry ---
        fm, raw_body = parse_frontmatter(text)
        sidecar = parse_sidecar_block(raw_body)
        body_clean = re.sub(r"<!--\s*(RESEARCH|SIDECAR)\b.*?-->", "", raw_body, flags=re.S)
        body_clean = re.sub(r"\n```\s*$", "\n", body_clean).strip() + "\n"

        problems = validate_entry(fm, raw_body, body_clean)
        if problems:
            plist = "\n".join(f"- {p}" for p in problems)
            log(f"structural validation FAILED (round {rounds}): {problems}")
            followup = (
                prompt + "\n\n# Your latest output\n\n" + text
                + "\n\n# This draft is NOT publishable yet\n\n"
                "A mechanical check found these problems:\n" + plist
                + "\n\nRe-output the COMPLETE entry, fixed: valid frontmatter "
                "(title/date/tags/summary), a body that ends with a complete "
                "sentence, balanced ``` fences, and every <!-- SIDECAR --> / "
                "<!-- RESEARCH --> block closed with -->. Output the whole "
                "entry, not a diff or a description of the fix."
            )
            text = call_backend(followup, label=f"{CONFIG['bot_name']}_journal_fix{rounds+1}")
            rounds += 1
            continue

        # --- (c) forced self-review: re-read exactly what will publish ---
        fm = stamp_canonical_date(fm, today.strftime("%Y-%m-%d"))
        title = fm.get("title", "Untitled")
        tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
        summary = fm.get("summary", "")
        slug = (sidecar.get("output_meta") or {}).get("slug") or slugify(title)
        rendered = render_entry(title, fm["date"], tags, summary, body_clean)

        approved, revised = self_review(rendered, str(rounds + 1))
        rounds += 1
        if approved:
            log("self-review: APPROVED — publishing")
            break
        if revised and revised.strip():
            log(f"self-review: REVISE — applying {len(revised)} char correction")
            text = revised
        else:
            log("self-review: REVISE but no corrected entry returned — requesting full re-output")
            text = call_backend(
                prompt + "\n\n# Re-write\n\nYour review found problems but did "
                "not include a corrected entry. Output the COMPLETE corrected "
                "entry now — frontmatter and finished body.",
                label=f"{CONFIG['bot_name']}_journal_rewrite{rounds}")
        continue

    # ---- Publish the approved entry ------------------------------------
    rendered = render_entry(title, fm["date"], tags, summary, body_clean)
    dest = (DRAFTS_DIR if args.dry_run else PUBLISHED_DIR) / f"{fm['date']}-{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered)
    log(f"wrote entry: {dest}")

    # Side effects. Shell already ran (once, deduped) during gathering and was
    # fed back to the model, so there is no separate publish-time shell pass.
    safe_apply_writes(sidecar.get("writes", []))
    send_journal_emails(sidecar.get("emails", []))
    safe_persist_tasks(sidecar.get("tasks", []))

    if args.dry_run:
        log("dry-run: skipping index/site update")
        return 0

    past_entries.insert(0, {
        "date": fm["date"], "slug": slug, "title": title,
        "tags": tags, "summary": summary,
    })
    save_index(past_entries)
    presentation_self_check()
    regenerate_site(past_entries)
    _record_run(today)
    log(f"=== run done: published + self-reviewed after {rounds} rounds ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
