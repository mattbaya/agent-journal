#!/usr/bin/env python3
"""agent-journal task runner.

Runs on a 15-min cron tick (via the task-runner wrapper). Picks up
tasks from journal/tasks/pending/ whose schedule is ready and hands
their prompt to the configured backend. Same backend abstraction as
writer.py — no provider imports here.

Output actions:
  - publish     entry goes through the same path as a daily entry
  - draft       output saved to journal/drafts/
  - tools       output saved as a file in journal/tools/
  - update_continuity  output appended to journal/continuity.md
  - email       output emailed to output_meta.to (or matt_bcc fallback)
"""
import argparse
import fcntl
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .backends import load_backend


CONFIG: dict = {}
SECRETS: dict = {}
BACKEND = None

JOURNAL_DIR: Path = None
PENDING_DIR: Path = None
DONE_DIR: Path = None
FAILED_DIR: Path = None
PUBLISHED_DIR: Path = None
DRAFTS_DIR: Path = None
TOOLS_DIR: Path = None
CONTINUITY_PATH: Path = None
INDEX_PATH: Path = None
SITE_DIR: Path = None
LOG_PATH: Path = None
LOCK_PATH: Path = None
INVOCATIONS_LOG: Path = None

# "agent" is a tool-capable self-delegation action handled OUT OF BAND by
# ralph's journal_task_bridge.py (sudo, full shell/file tools, sandboxed). This
# text-only runner recognizes it as valid but never executes it: agent tasks
# are filtered out in main() and left in pending/ for the bridge to pick up.
TEXT_ACTIONS = {"publish", "draft", "tools", "update_continuity", "email"}
AGENT_ACTION = "agent"
VALID_ACTIONS = TEXT_ACTIONS | {AGENT_ACTION}


def setup_paths(cfg):
    global JOURNAL_DIR, PENDING_DIR, DONE_DIR, FAILED_DIR
    global PUBLISHED_DIR, DRAFTS_DIR, TOOLS_DIR, CONTINUITY_PATH, INDEX_PATH
    global SITE_DIR, LOG_PATH, LOCK_PATH, INVOCATIONS_LOG
    JOURNAL_DIR = Path(cfg["journal_dir"]).resolve()
    PENDING_DIR = JOURNAL_DIR / "tasks" / "pending"
    DONE_DIR = JOURNAL_DIR / "tasks" / "done"
    FAILED_DIR = JOURNAL_DIR / "tasks" / "failed"
    PUBLISHED_DIR = JOURNAL_DIR / "published"
    DRAFTS_DIR = JOURNAL_DIR / "drafts"
    TOOLS_DIR = JOURNAL_DIR / "tools"
    CONTINUITY_PATH = JOURNAL_DIR / "continuity.md"
    INDEX_PATH = JOURNAL_DIR / "index.json"
    SITE_DIR = Path(cfg["web_dir"]).resolve() if cfg.get("web_dir") else None
    LOG_PATH = JOURNAL_DIR / "logs" / "task_runner.log"
    LOCK_PATH = JOURNAL_DIR / ".task_runner.lock"
    INVOCATIONS_LOG = JOURNAL_DIR / "logs" / "invocations.jsonl"


def log(msg):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n"
    print(line, end="")
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass


def log_invocation(label, usage, cost, duration_ms):
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
    }
    try:
        with open(INVOCATIONS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def cron_fires_too_often(cron_expr, max_per_hour=4):
    try:
        from croniter import croniter
        from datetime import timedelta as _td
        now = datetime.now(timezone.utc)
        it = croniter(cron_expr, now)
        fires = 0
        end = now + _td(hours=1)
        nt = it.get_next(datetime)
        while nt <= end:
            fires += 1
            if fires > max_per_hour:
                return True
            nt = it.get_next(datetime)
    except Exception:
        return True
    return False


def is_ready(task, now):
    schedule = task.get("schedule_type")
    if schedule == "one_time":
        run_at = task.get("run_at")
        if not run_at:
            return False
        try:
            ra = datetime.fromisoformat(run_at)
        except ValueError:
            return False
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc)
        return ra <= now
    if schedule == "recurring":
        cron = task.get("cron")
        if not cron or cron_fires_too_often(cron):
            return False
        from croniter import croniter
        from datetime import timedelta as _td
        last = task.get("last_run_at")
        try:
            prev_tick = croniter(cron, now).get_prev(datetime)
        except Exception:
            return False
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt >= prev_tick:
                    return False
            except ValueError:
                pass
        return (now - prev_tick) <= _td(minutes=15)
    return False


def slugify(s):
    out = re.sub(r"[^a-zA-Z0-9\s-]", "", s or "").strip().lower()
    out = re.sub(r"\s+", "-", out)
    return out[:60] or "untitled"


def parse_frontmatter(text):
    text = text.lstrip()
    text = re.sub(r"^```ya?ml\s*\n", "---\n", text)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
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


def action_publish(task, output, now):
    fm, body = parse_frontmatter(output)
    if not fm.get("title"):
        meta = task.get("output_meta") or {}
        fm = {
            "title": meta.get("title_hint") or f"Task: {task['id']}",
            "tags": meta.get("tags") or ["task"],
            "summary": meta.get("title_hint") or "(generated by task runner)",
        }
        body = output
    fm["date"] = now.strftime("%Y-%m-%d")
    title = fm["title"]
    slug = (task.get("output_meta") or {}).get("slug") or slugify(title)
    dest = PUBLISHED_DIR / f"{fm['date']}-{slug}.md"
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "---\n"
        + f'title: {json.dumps(title)}\n'
        + f"date: {fm['date']}\n"
        + "tags: " + json.dumps(fm.get("tags") or []) + "\n"
        + f"summary: {json.dumps(fm.get('summary') or '')}\n"
        + "---\n\n"
        + body.strip()
        + "\n"
    )
    log(f"published task entry: {dest.name}")
    try:
        idx = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
    except json.JSONDecodeError:
        idx = []
    idx.insert(0, {"date": fm["date"], "slug": slug, "title": title,
                   "tags": fm.get("tags") or [], "summary": fm.get("summary") or ""})
    INDEX_PATH.write_text(json.dumps(idx, indent=2))
    try:
        from . import site as site_module
        if SITE_DIR:
            site_module.build_site(PUBLISHED_DIR, SITE_DIR, idx, CONFIG)
    except Exception as e:
        log(f"site regen FAILED: {e}")


def action_draft(task, output, now):
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    meta = task.get("output_meta") or {}
    slug = meta.get("slug") or slugify(meta.get("title_hint") or task["id"])
    (DRAFTS_DIR / f"{now.strftime('%Y-%m-%d')}-{slug}.md").write_text(output.strip() + "\n")


def action_tools(task, output, now):
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    meta = task.get("output_meta") or {}
    fname = meta.get("slug") or f"task-{task['id']}-{now.strftime('%Y%m%d%H%M%S')}.txt"
    if not fname.endswith((".py", ".sh", ".txt", ".md", ".json")):
        fname += ".txt"
    (TOOLS_DIR / fname).write_text(output)


def action_update_continuity(task, output, now):
    existing = CONTINUITY_PATH.read_text() if CONTINUITY_PATH.exists() else ""
    appended = (
        existing.rstrip()
        + "\n\n"
        + f"## Appended by task `{task['id']}` on {now.strftime('%Y-%m-%d')}\n\n"
        + output.strip()
        + "\n"
    )
    CONTINUITY_PATH.write_text(appended)


def action_email(task, output, now):
    import smtplib
    import ssl
    import email.utils
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    meta = task.get("output_meta") or {}
    to_addr = (meta.get("to") or CONFIG.get("matt_bcc") or "").strip()
    if not to_addr:
        raise RuntimeError("email action: no recipient (set output_meta.to or matt_bcc)")
    subject = meta.get("title_hint") or f"[{CONFIG.get('bot_name', 'agent')} task] {task['id']}"
    smtp_host = CONFIG.get("smtp_server")
    smtp_port = int(CONFIG.get("smtp_port") or 465)
    pw_key = CONFIG.get("email_password_secret", "EMAIL_PASSWORD")
    smtp_pass = SECRETS.get(pw_key)
    from_addr = CONFIG.get("bot_email")
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText(output, "plain", "utf-8"))
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as s:
        s.login(from_addr, smtp_pass)
        s.sendmail(from_addr, [to_addr], msg.as_string())


ACTIONS = {
    "publish": action_publish,
    "draft": action_draft,
    "tools": action_tools,
    "update_continuity": action_update_continuity,
    "email": action_email,
}


def run_task(task_path, now, dry_run):
    try:
        task = json.loads(task_path.read_text())
    except Exception as e:
        log(f"load {task_path.name} failed: {e}")
        return False
    tid = task.get("id") or task_path.stem
    action = task.get("output_action")
    if action not in VALID_ACTIONS:
        log(f"task {tid}: invalid output_action {action!r}")
        return False
    prompt = (task.get("prompt") or "").strip()
    if not prompt:
        log(f"task {tid}: empty prompt")
        return False
    max_tokens = min(
        int(task.get("max_tokens") or CONFIG.get("max_tokens_default", 12000)),
        int(CONFIG.get("max_tokens_ceiling", 24000)),
    )
    log(f"running task {tid} action={action} max_tokens={max_tokens}")
    if dry_run:
        return True
    started = time.time()
    try:
        text, usage, cost = BACKEND.call_llm(prompt, max_tokens=max_tokens)
    except Exception as e:
        log(f"  backend call FAILED: {e}")
        return False
    log_invocation(f"task_{action}_{tid}", usage, cost, int((time.time() - started) * 1000))
    log(f"  llm returned {len(text)} chars, cost=${cost:.4f}")
    try:
        ACTIONS[action](task, text, now)
    except Exception as e:
        log(f"  action handler FAILED: {e}")
        return False
    task["status"] = "done" if task.get("schedule_type") == "one_time" else "pending"
    task["last_run_at"] = now.isoformat()
    if task.get("schedule_type") == "recurring":
        task_path.write_text(json.dumps(task, indent=2))
    return True


def main():
    global CONFIG, SECRETS, BACKEND
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--secrets-stdin", action="store_true")
    ap.add_argument("--secrets-file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-id")
    args = ap.parse_args()

    CONFIG = json.loads(Path(args.config).read_text())
    if args.secrets_stdin:
        SECRETS = json.loads(sys.stdin.read() or "{}")
    elif args.secrets_file:
        SECRETS = json.loads(Path(args.secrets_file).read_text())

    setup_paths(CONFIG)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND = load_backend(CONFIG, SECRETS)

    lock_fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another task_runner running — exiting")
        return 0

    now = datetime.now(timezone.utc)
    log(f"=== runner start {now.isoformat()} backend={BACKEND.name} ===")
    ready = []
    agent_left = 0
    for tp in sorted(PENDING_DIR.glob("*.json")):
        try:
            task = json.loads(tp.read_text())
        except Exception:
            continue
        # Tool-capable self-delegation is executed by ralph's
        # journal_task_bridge.py, never by this text-only runner. Leave such
        # tasks untouched in pending/ (even under --force-id) so the bridge
        # owns their scheduling + lifecycle.
        if task.get("output_action") == AGENT_ACTION:
            agent_left += 1
            continue
        if args.force_id and task.get("id") == args.force_id:
            ready.append(tp)
            continue
        if not args.force_id and is_ready(task, now):
            ready.append(tp)
    log(f"  pending={len(list(PENDING_DIR.glob('*.json')))} ready={len(ready)}"
        + (f" agent_left={agent_left}" if agent_left else ""))

    for tp in ready:
        # Belt-and-suspenders: agent tasks are filtered out above, but never
        # move one to done/failed from this text-only runner.
        try:
            guard_task = json.loads(tp.read_text())
            if guard_task.get("output_action") == AGENT_ACTION:
                log(f"  SKIP agent task {tp.name} (should be handled by bridge)")
                continue
        except Exception:
            pass
        ok = run_task(tp, now, args.dry_run)
        if args.dry_run:
            continue
        try:
            task = json.loads(tp.read_text())
            if task.get("schedule_type") == "one_time":
                target = (DONE_DIR if ok else FAILED_DIR) / tp.name
                tp.rename(target)
        except Exception as e:
            log(f"  finalize failed: {e}")

    log("=== runner done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
