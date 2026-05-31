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

# Hard caps applied regardless of config
SHELL_DEFAULT_TIMEOUT = 30
SHELL_MAX_TIMEOUT = 300
SHELL_STDOUT_CAP = 8000
SHELL_STDERR_CAP = 2000
READ_MAX_BYTES = 100_000
RECENT_ENTRIES_IN_PROMPT = None  # None = no cap; show every past entry


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

    fields = {
        "date": today.strftime("%Y-%m-%d"),
        "weekday": today.strftime("%A"),
        "past_count": str(len(shown)),
        "past_block": past_block,
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
    }

    template = FALLBACK_PROMPT_TEMPLATE
    if PROMPT_PATH.exists():
        try:
            template = PROMPT_PATH.read_text()
        except Exception as e:
            log(f"WARN: read {PROMPT_PATH} failed: {e} — using fallback")

    try:
        return string.Template(template).safe_substitute(fields)
    except Exception as e:
        log(f"WARN: prompt substitution failed: {e} — fallback")
        return string.Template(FALLBACK_PROMPT_TEMPLATE).safe_substitute(fields)


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
    valid_actions = {"publish", "draft", "tools", "update_continuity", "email"}
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

def _interval_check(now: datetime) -> tuple:
    """Return (should_run, reason). Reads `journal_interval_hours` from
    CONFIG (default 24); compares now against the timestamp stored in
    LAST_RUN_PATH. The bot can edit `journal_interval_hours` in her own
    config.json via a `writes:` block — the next wrapper tick honors
    the new value.

    journal_interval_hours = 0 means "run every tick, no gating" (useful
    for trigger-driven setups where the wrapper is invoked only when a
    trigger fires).
    """
    raw = CONFIG.get("journal_interval_hours", 24)
    try:
        interval_secs = int(float(raw) * 3600)
    except (TypeError, ValueError):
        log(f"invalid journal_interval_hours={raw!r}; falling back to 24")
        interval_secs = 24 * 3600
    if interval_secs <= 0:
        return True, "journal_interval_hours <= 0 (no gating)"
    if not LAST_RUN_PATH.exists():
        return True, "no prior run recorded"
    try:
        last = int(LAST_RUN_PATH.read_text().strip())
    except (ValueError, OSError):
        return True, "stale or unreadable .last-journal-run; will overwrite"
    elapsed = int(now.timestamp()) - last
    if elapsed >= interval_secs:
        return True, f"elapsed {elapsed}s >= interval {interval_secs}s"
    remaining = interval_secs - elapsed
    return False, (f"interval not elapsed: {remaining}s remaining of "
                   f"{interval_secs}s (journal_interval_hours={raw})")


def _record_run(now: datetime) -> None:
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(str(int(now.timestamp())))


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> int:
    global CONFIG, SECRETS, BACKEND

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to config.json")
    ap.add_argument("--secrets-stdin", action="store_true",
                    help="Read JSON secrets dict from stdin")
    ap.add_argument("--secrets-file", help="Path to JSON secrets file")
    ap.add_argument("--dry-run", action="store_true",
                    help="Write to drafts/ instead of published/; no commit, no site")
    ap.add_argument("--max-research-rounds", type=int, default=0,
                    help="Hard cap on followup rounds; 0 (default) = no cap. "
                         "Use a positive value as an ops-level circuit breaker.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore journal_interval_hours gating and run now.")
    args = ap.parse_args()

    CONFIG = load_config(Path(args.config))
    if args.secrets_stdin:
        SECRETS = load_secrets_from_stdin()
    elif args.secrets_file:
        SECRETS = json.loads(Path(args.secrets_file).read_text())
    else:
        SECRETS = {}

    setup_paths(CONFIG)
    BACKEND = load_backend(CONFIG, SECRETS)

    today = datetime.now()

    # Interval gating: skip if it's been less than journal_interval_hours
    # since the last successful run, unless --force is passed.
    if not args.force and not args.dry_run:
        should_run, reason = _interval_check(today)
        if not should_run:
            log(f"skip: {reason}")
            return 0
        log(f"interval check: {reason}")

    log(f"=== run start {today.isoformat()} backend={BACKEND.name} model={BACKEND.model} dry_run={args.dry_run} ===")

    past_entries = load_index()
    continuity = CONTINUITY_PATH.read_text() if CONTINUITY_PATH.exists() else ""
    drafts = list_dir_files(DRAFTS_DIR)
    ideas = list_dir_files(IDEAS_DIR)
    tools = list_dir_files(TOOLS_DIR)

    prompt = build_prompt(today, past_entries, continuity, drafts, ideas, tools)

    text = call_backend(prompt, label=f"{CONFIG['bot_name']}_journal")
    log(f"first-pass response: {len(text)} chars")

    # Followup rounds for research/reads/shell
    rounds = 0
    while True:
        queries = parse_research_block(text)
        partial = parse_sidecar_block(text) if "<!--" in text else {}
        reads = partial.get("reads", []) if isinstance(partial, dict) else []
        shells = partial.get("shell", []) if isinstance(partial, dict) else []
        if not queries and not reads and not shells:
            break
        extras = []
        if queries:
            extras.append(run_research(queries))
        if reads:
            r = safe_read_paths(reads)
            b = ["# Files you asked to read", ""]
            for item in r:
                b.append(f"## {item['path']}")
                b.append("```" if item["ok"] else "")
                b.append(item["content"])
                if item["ok"]:
                    b.append("```")
                b.append("")
            extras.append("\n".join(b))
        if shells:
            shell_out = safe_run_shell(shells)
            b = ["# Shell commands you ran", ""]
            for r in shell_out:
                b.append(f"## $ {r['cmd']}")
                b.append(f"exit={r['exit_code']}  {r['duration_ms']}ms"
                         + ("  TIMED OUT" if r.get("timed_out") else ""))
                if r["stdout"]:
                    b.append("stdout:")
                    b.append("```")
                    b.append(r["stdout"])
                    b.append("```")
                if r["stderr"]:
                    b.append("stderr:")
                    b.append("```")
                    b.append(r["stderr"])
                    b.append("```")
                b.append("")
            extras.append("\n".join(b))
        if args.max_research_rounds and rounds + 1 >= args.max_research_rounds:
            floor_text = (
                "\n\nThis is your last round — the --max-research-rounds "
                f"circuit breaker ({args.max_research_rounds}) has been reached. "
                "Any further RESEARCH/reads:/shell: blocks will be ignored — "
                "write the final entry now."
            )
        else:
            floor_text = (
                "\n\nIf you still need more material, request it and there "
                "will be another round. Otherwise write the final entry now."
            )
        followup = (prompt + "\n\n# Your first-pass output\n\n" + text + "\n\n"
                    + "\n\n".join(extras)
                    + floor_text)
        text = call_backend(followup, label=f"{CONFIG['bot_name']}_journal_round{rounds+2}")
        log(f"post-followup response: {len(text)} chars (round {rounds + 1} complete)")
        rounds += 1
        if args.max_research_rounds and rounds >= args.max_research_rounds:
            log(f"hit --max-research-rounds cap of {args.max_research_rounds}; forcing exit")
            break

    # Parse final entry
    fm, body = parse_frontmatter(text)
    if not fm or "title" not in fm:
        log("ERROR: no valid frontmatter — saving raw to drafts/")
        fallback = DRAFTS_DIR / f"{today.strftime('%Y-%m-%d')}-unparseable.md"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(text)
        return 2

    fm = stamp_canonical_date(fm, today.strftime("%Y-%m-%d"))
    title = fm.get("title", "Untitled")
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    summary = fm.get("summary", "")

    sidecar = parse_sidecar_block(body)
    body_clean = re.sub(r"<!--\s*(RESEARCH|SIDECAR)\b.*?-->", "", body, flags=re.S)
    body_clean = re.sub(r"\n```\s*$", "\n", body_clean).strip() + "\n"

    slug = (sidecar.get("output_meta") or {}).get("slug") or slugify(title)
    dest = (DRAFTS_DIR if args.dry_run else PUBLISHED_DIR) / f"{fm['date']}-{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "---\n"
        + f'title: {json.dumps(title)}\n'
        + f"date: {fm['date']}\n"
        + "tags: " + json.dumps(tags) + "\n"
        + f"summary: {json.dumps(summary)}\n"
        + "---\n\n"
        + body_clean
    )
    log(f"wrote entry: {dest}")

    # Side effects
    safe_apply_writes(sidecar.get("writes", []))
    safe_run_shell(sidecar.get("shell", []))
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
    regenerate_site(past_entries)
    _record_run(today)
    log("=== run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
