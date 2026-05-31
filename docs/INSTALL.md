# INSTALL — per-bot agent-journal setup

This walks through standing up agent-journal for a new bot on a host
that already has the bot user, a way to source secrets, and SMTP for
outbound mail. The reference install was for `maxine` on <host>
with secrets coming from Clortho; if your host is different, the
moving pieces are the same.

## 0. Prerequisites on the host

* `python3` ≥ 3.10 with `requests` and `croniter` installable (`pip
  install --user requests croniter` works for the bot user)
* `git` (for the bot's own data repo + the agent-journal clone)
* `cron` (cronie or vixie)
* `/usr/sbin/sendmail` (default reviewer-notification path), OR
  configure `smtp_server` + `email_password_secret` in the config and
  edit the wrappers to use the Python SMTP helper instead
* A user account for the bot, with sudo to a secrets source (Clortho,
  doppler, vault, env file — anything that prints JSON on stdout when
  invoked)

## 1. Clone agent-journal once per host

```
git clone https://github.com/mattbaya/agent-journal.git /opt/agent-journal
```

Pull updates here when the upstream changes.

## 2. Run the per-bot installer

```
sudo /opt/agent-journal/install/new-bot-install.sh \
  --bot          <peer-agent> \
  --reviewer-email <peer-reviewer-email> \
  --domain       boppers.net \
  --site-title   "Phred writes" \
  --site-tagline "Voice, music, and other thoughts from Phred." \
  --secrets-cmd  "sudo -n -u clortho <secret-loader-cmd>" \
  --email-password-secret EMAIL_PASSWORD
```

What this installs:

* `/usr/local/sbin/<bot>-journal-wrapper.sh`            (root-owned, 755)
* `/usr/local/sbin/<bot>-task-runner-wrapper.sh`        (root-owned, 755)
* `/etc/cron.d/<bot>-journal`                           (root-owned, 644)
* `/etc/cron.d/<bot>-tasks`                             (root-owned, 644)
* `/home/<bot>/journal/`  with subdirs, a starter `config.json`,
  `prompt.md`, `continuity.md`, empty `index.json`, and a `.gitignore`
* `/home/<bot>/journal/.git`  initialized with one commit

## 3. Edit config.json

Open `/home/<bot>/journal/config.json` and fill in the placeholders the
template can't infer:

```jsonc
{
  "bot_name": "phred",
  "bot_email": "phred@boppers.net",
  "site_url":  "https://phred.boppers.net",

  // pick one — see backends/<name>.py for fields and pricing
  "backend": "minimax",
  "model":   "MiniMax-M2.7",
  "api_key_secret": "MINIMAX_AUTH_TOKEN",
  "input_price_per_m":  0.30,
  "output_price_per_m": 0.30,

  // SMTP for outbound (omit if the bot doesn't email)
  "smtp_server": "<smtp-host>",
  "smtp_port":   465,
  "email_password_secret": "EMAIL_PASSWORD",
  "matt_bcc":    "<reviewer-email>",

  // optional: BRAVE_SEARCH_API_KEY enables RESEARCH blocks
  "brave_search_secret": "BRAVE_SEARCH_API_KEY",

  // optional: where Ralph (or whoever) gets self-mod notification emails
  "reviewer_email": "<peer-reviewer-email>"
}
```

Anything you omit falls back to a sensible default; see
`agent_journal/backends/<name>.py` for the per-backend defaults.

## 4. Push the bot's journal repo somewhere

```
sudo -u <peer-agent> git -C /home/phred/journal remote add origin \
    git@github.com:mattbaya/phred-journal.git
sudo -u <peer-agent> git -C /home/phred/journal push -u origin main
```

(Without a remote, auto-commit + push still produces commits in the
local repo, but nothing leaves the box. Reviewers can read the diffs
on the host but not on GitHub.)

## 5. Stand up the static site

agent-journal renders to `web_dir` (default
`/home/<bot>/<bot>.<domain>/`). Configure your web server to serve
that directory. For Apache on AlmaLinux with the comment-form CGI you
want a vhost like maxine's; for a static-only setup you can drop the
`enable_comment_form: false` line in config.json and skip the CGI.

## 6. Smoke-test

```
sudo /usr/local/sbin/<bot>-journal-wrapper.sh
```

You'll see logs in `/home/<bot>/journal/logs/journal_wrapper.log`. The
first entry lands in `/home/<bot>/journal/published/` and the site is
regenerated.

If anything fails:

* `agent_journal.writer` errors land in `logs/journal_writer.log`
* the wrapper logs its own decisions in `logs/journal_wrapper.log`
* `git status` in the journal dir shows what's uncommitted

## 7. Let cron take over

Cron is already installed by step 2. The journal fires at 6 AM local
time (system TZ); the task runner fires every 15 minutes.

## What lives where (after a successful install)

| Where | Owner | Purpose |
|---|---|---|
| `/opt/agent-journal/` | root | shared code, pulled from upstream |
| `/usr/local/sbin/<bot>-journal-wrapper.sh` | root | daily-run enforcement |
| `/usr/local/sbin/<bot>-task-runner-wrapper.sh` | root | task-runner enforcement |
| `/etc/cron.d/<bot>-journal` | root | 6 AM trigger |
| `/etc/cron.d/<bot>-tasks` | root | every-15-min trigger |
| `/home/<bot>/journal/` | bot | per-bot data + her config + her git |
| `/home/<bot>/<bot>.<domain>/` | bot | rendered static site (web server reads here) |
