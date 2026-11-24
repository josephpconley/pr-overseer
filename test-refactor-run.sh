#!/bin/bash
# Weekly test-suite review/refactor (cron: Sunday 9pm). review.py takes .overseer.lock itself
# (waits up to 30 min for a running review tick), so no outer flock here.
# Node/pnpm for repo installs, suites and git hooks (same PATH as cron-run.sh).
ROOT=$(cd "$(dirname "$0")" && pwd)
export PATH="$HOME/.nvm/versions/node/v24.22.3/bin:$HOME/.local/share/pnpm:$HOME/bin:$HOME/.local/bin:$PATH"
# Appends timestamped output to logs/test-refactor.log ONLY when the run acted or warned:
# non-zero exit, a digest email / opened PR line, or any "!" warning/error line.
LOG=$ROOT/logs/test-refactor.log
TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT
if command -v pr-overseer-review >/dev/null 2>&1; then
  pr-overseer-review --test-refactor --live >"$TMP" 2>&1
else
  python3 "$ROOT/review.py" --test-refactor --live >"$TMP" 2>&1
fi
rc=$?
if [ "$rc" -ne 0 ] || grep -qE '^(Digest email:|Test-refactor PRs opened:)' "$TMP" || grep -qE '^\s*!|^warning:' "$TMP"; then
  { echo "===== test-refactor run start ====="; cat "$TMP"; echo "===== test-refactor run end (exit $rc) ====="; } \
    | while IFS= read -r line; do printf '%s %s\n' "$(date -Iseconds)" "$line"; done >> "$LOG"
fi
