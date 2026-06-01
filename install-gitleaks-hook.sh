#!/bin/bash
# Install a gitleaks pre-commit hook in this repo. Idempotent — re-run
# any time. Run from the repo root.
#
# What it does:
#   - Writes a hook to .git/hooks/pre-commit that runs gitleaks against
#     each staged file (gitleaks dir, per-file).
#   - The repo's .gitleaks.toml provides the allowlist that suppresses
#     known false positives (honeypot bait, opt-share doc placeholders).
#
# Why per-file vs `gitleaks git --pre-commit --staged`:
#   In gitleaks v8.30.1 the `git --pre-commit --staged` path doesn't
#   reliably trigger rules on diff content (verified empirically).
#   Scanning each staged file with `gitleaks dir` works around that and
#   is fast (only touches files actually changed by the commit).
#
# Requires:
#   - /opt/share/bin/gitleaks installed (v8.30.1 tested).
#
# Bypass for emergencies:
#   git commit --no-verify   (use sparingly; bypasses ALL hooks)

set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "ERROR: not inside a git repo"
    exit 1
}

GITLEAKS_BIN=/opt/share/bin/gitleaks
if [ ! -x "$GITLEAKS_BIN" ]; then
    echo "ERROR: $GITLEAKS_BIN not found or not executable"
    exit 1
fi

HOOK="$REPO_ROOT/.git/hooks/pre-commit"
mkdir -p "$(dirname "$HOOK")"

cat > "$HOOK" <<'HOOK_EOF'
#!/bin/bash
# Pre-commit: block commits that introduce secrets.
# Installed by scripts/install-gitleaks-hook.sh.

set -uo pipefail

GITLEAKS_BIN=/opt/share/bin/gitleaks
if [ ! -x "$GITLEAKS_BIN" ]; then
    echo "warning: gitleaks not found at $GITLEAKS_BIN; skipping secret scan"
    exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
cd "$REPO_ROOT"

# Pick the gitleaks config if present; otherwise gitleaks uses defaults.
CONFIG_ARGS=""
if [ -f "$REPO_ROOT/.gitleaks.toml" ]; then
    CONFIG_ARGS="--config $REPO_ROOT/.gitleaks.toml"
fi

# Collect staged files being added or modified (skip deletions).
FILES=$(git diff --cached --name-only --diff-filter=AM)
if [ -z "$FILES" ]; then
    exit 0
fi

REPORT=$(mktemp)
trap 'rm -f "$REPORT"' EXIT

ANY_LEAKS=0
while IFS= read -r f; do
    [ -f "$f" ] || continue
    $GITLEAKS_BIN dir --no-banner $CONFIG_ARGS \
        --report-format=json --report-path="$REPORT" \
        "$f" >/dev/null 2>&1 || true
    if [ -s "$REPORT" ] && [ "$(cat "$REPORT")" != "[]" ] && [ "$(cat "$REPORT")" != "null" ]; then
        python3 -c "
import json
d = json.load(open('$REPORT'))
for x in d:
    desc = (x.get('Description') or '').strip()[:90]
    print(f\"  ✗ {x.get('File')}:{x.get('StartLine')}  rule={x.get('RuleID')}  {desc}\")
" 2>/dev/null
        ANY_LEAKS=1
    fi
done <<< "$FILES"

if [ "$ANY_LEAKS" -ne 0 ]; then
    echo ""
    echo "gitleaks blocked this commit — secrets detected in staged files."
    echo "Options:"
    echo "  1. Remove the secret from the file and stage again."
    echo "  2. If it's a false positive, add an allowlist entry to .gitleaks.toml."
    echo "  3. Emergency bypass: git commit --no-verify  (last resort)"
    exit 1
fi

exit 0
HOOK_EOF

chmod 0755 "$HOOK"
echo "✓ installed $HOOK"
echo "  gitleaks: $($GITLEAKS_BIN version)"
echo "  config:   $REPO_ROOT/.gitleaks.toml $([ -f "$REPO_ROOT/.gitleaks.toml" ] || echo '(absent — using defaults)')"
