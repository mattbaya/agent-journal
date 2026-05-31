#!/bin/bash
#
# Per-bot installer for agent-journal. Run as root after the
# agent_journal Python package is installed (typically under
# /opt/agent-journal/).
#
# Usage:
#   sudo ./install/new-bot-install.sh \
#     --bot <peer-agent> \
#     --reviewer-email <peer-reviewer-email> \
#     --secrets-cmd "sudo -n -u clortho <secret-loader-cmd>"
#
# What it does:
#   1. Substitutes @@TOKEN@@ placeholders in the wrapper + cron templates
#      and installs them to /usr/local/sbin/ and /etc/cron.d/.
#   2. Drops a starter config.json into /home/<bot>/journal/ and
#      copies the prompt template + a minimal continuity.md + README.md.
#   3. Initializes a git repo in /home/<bot>/journal/ owned by the bot,
#      with the standard .gitignore.
#
# Does NOT do:
#   - Apache vhost (host-specific; you set this up alongside)
#   - The bot's email account or mailbox (assumes you have that)
#   - Editing the bot's config.json beyond the placeholder substitutions
#     — you must edit the resulting file to set backend, pricing, secrets
#     key names, site title/tagline, etc.

set -euo pipefail

# Parse args
BOT=
REVIEWER_EMAIL=<peer-reviewer-email>
SECRETS_CMD="sudo -n -u clortho <secret-loader-cmd>"
AGENT_JOURNAL_PATH=/opt/agent-journal
DOMAIN=boppers.net
SITE_TITLE=
SITE_TAGLINE=
EMAIL_PASSWORD_SECRET=EMAIL_PASSWORD

while [ $# -gt 0 ]; do
    case "$1" in
        --bot)              BOT=$2; shift 2;;
        --reviewer-email)   REVIEWER_EMAIL=$2; shift 2;;
        --secrets-cmd)      SECRETS_CMD=$2; shift 2;;
        --agent-journal-path) AGENT_JOURNAL_PATH=$2; shift 2;;
        --domain)           DOMAIN=$2; shift 2;;
        --site-title)       SITE_TITLE=$2; shift 2;;
        --site-tagline)     SITE_TAGLINE=$2; shift 2;;
        --email-password-secret) EMAIL_PASSWORD_SECRET=$2; shift 2;;
        *) echo "unknown arg: $1"; exit 2;;
    esac
done

if [ -z "$BOT" ]; then
    echo "missing --bot"; exit 2
fi
[ -z "$SITE_TITLE" ]   && SITE_TITLE="$BOT writes"
[ -z "$SITE_TAGLINE" ] && SITE_TAGLINE="Daily notes from $BOT, an AI agent."

JOURNAL_DIR=/home/$BOT/journal
CONFIG_PATH=$JOURNAL_DIR/config.json
INSTALL_DIR=$(dirname "$(readlink -f "$0")")
REPO_ROOT=$(dirname "$INSTALL_DIR")

substitute () {
    sed -e "s|@@BOT_NAME@@|$BOT|g" \
        -e "s|@@JOURNAL_DIR@@|$JOURNAL_DIR|g" \
        -e "s|@@AGENT_JOURNAL_PATH@@|$AGENT_JOURNAL_PATH|g" \
        -e "s|@@CONFIG_PATH@@|$CONFIG_PATH|g" \
        -e "s|@@REVIEWER_EMAIL@@|$REVIEWER_EMAIL|g" \
        -e "s|@@SECRETS_CMD@@|$SECRETS_CMD|g" \
        -e "s|@@DOMAIN@@|$DOMAIN|g" \
        -e "s|@@SITE_TITLE@@|$SITE_TITLE|g" \
        -e "s|@@SITE_TAGLINE@@|$SITE_TAGLINE|g" \
        -e "s|@@EMAIL_PASSWORD_SECRET@@|$EMAIL_PASSWORD_SECRET|g"
}

echo "Installing agent-journal for $BOT"
echo "  journal dir:    $JOURNAL_DIR"
echo "  config:         $CONFIG_PATH"
echo "  reviewer:       $REVIEWER_EMAIL"
echo "  agent_journal:  $AGENT_JOURNAL_PATH"
echo ""

# 1. Install wrappers + crons
substitute < "$INSTALL_DIR/journal-wrapper.sh.template" \
  > "/usr/local/sbin/$BOT-journal-wrapper.sh"
chmod 755 "/usr/local/sbin/$BOT-journal-wrapper.sh"

substitute < "$INSTALL_DIR/task-runner-wrapper.sh.template" \
  > "/usr/local/sbin/$BOT-task-runner-wrapper.sh"
chmod 755 "/usr/local/sbin/$BOT-task-runner-wrapper.sh"

substitute < "$INSTALL_DIR/cron-journal.template" \
  > "/etc/cron.d/$BOT-journal"
chmod 644 "/etc/cron.d/$BOT-journal"

substitute < "$INSTALL_DIR/cron-tasks.template" \
  > "/etc/cron.d/$BOT-tasks"
chmod 644 "/etc/cron.d/$BOT-tasks"

# 2. Per-bot journal dir
install -d -o "$BOT" -g "$BOT" -m 0755 "$JOURNAL_DIR" "$JOURNAL_DIR/logs"
install -d -o "$BOT" -g "$BOT" -m 0755 \
    "$JOURNAL_DIR/published" "$JOURNAL_DIR/drafts" \
    "$JOURNAL_DIR/ideas" "$JOURNAL_DIR/tools" \
    "$JOURNAL_DIR/tasks/pending" "$JOURNAL_DIR/tasks/done" "$JOURNAL_DIR/tasks/failed" \
    "$JOURNAL_DIR/inbox"

if [ ! -f "$CONFIG_PATH" ]; then
    substitute < "$INSTALL_DIR/config.json.template" > "$CONFIG_PATH"
    chown "$BOT:$BOT" "$CONFIG_PATH"
fi

if [ ! -f "$JOURNAL_DIR/prompt.md" ]; then
    install -o "$BOT" -g "$BOT" -m 0644 \
        "$REPO_ROOT/agent_journal/prompt.md.template" \
        "$JOURNAL_DIR/prompt.md"
fi
if [ ! -f "$JOURNAL_DIR/continuity.md" ]; then
    install -o "$BOT" -g "$BOT" -m 0644 \
        "$REPO_ROOT/docs/continuity.md.starter" \
        "$JOURNAL_DIR/continuity.md"
fi
if [ ! -f "$JOURNAL_DIR/index.json" ]; then
    echo "[]" > "$JOURNAL_DIR/index.json"
    chown "$BOT:$BOT" "$JOURNAL_DIR/index.json"
fi

if [ ! -f "$JOURNAL_DIR/.gitignore" ]; then
    cat >"$JOURNAL_DIR/.gitignore" <<'EOF'
logs/
ideas/
tools/
inbox/
.task_runner.lock
__pycache__/
*.pyc
EOF
    chown "$BOT:$BOT" "$JOURNAL_DIR/.gitignore"
fi

# 3. Init git repo for this bot's data (caller pushes to their own remote)
if [ ! -d "$JOURNAL_DIR/.git" ]; then
    sudo -u "$BOT" git -C "$JOURNAL_DIR" init -b main
    sudo -u "$BOT" git -C "$JOURNAL_DIR" add -A
    sudo -u "$BOT" git -C "$JOURNAL_DIR" \
        -c user.email="$BOT@$DOMAIN" \
        -c user.name="$BOT" \
        commit -m "initial: agent-journal setup for $BOT"
fi

echo ""
echo "Done. Next steps:"
echo "  1. Edit $CONFIG_PATH to set backend, pricing, secrets, etc."
echo "  2. Add a git remote: sudo -u $BOT git -C $JOURNAL_DIR remote add origin <url>"
echo "  3. Set up a vhost for $BOT.$DOMAIN pointing at the web_dir."
echo "  4. Smoke test:"
echo "       sudo /usr/local/sbin/$BOT-journal-wrapper.sh"
