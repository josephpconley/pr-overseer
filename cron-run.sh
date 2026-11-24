#!/bin/bash
# Node/pnpm for repo git hooks (e.g. a pre-commit hook that runs pnpm)
ROOT=$(cd "$(dirname "$0")" && pwd)
export PATH="$HOME/.nvm/versions/node/v24.22.3/bin:$HOME/.local/share/pnpm:$HOME/bin:$HOME/.local/bin:$PATH"
# Run pr-overseer-review; append timestamped output to logs/cron.log ONLY when there was action:
# non-zero exit, "Reviews written:" (review.py success summary), or any "!" warning/error line.
# Idle scans ("No reviews written." with no warnings) write nothing.
LOG=$ROOT/logs/cron.log
exec 9>"$ROOT/.cron.lock"
flock -n 9 || exit 0   # previous tick still running: skip silently
TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT
if command -v pr-overseer-review >/dev/null 2>&1; then
  pr-overseer-review --max-reviews 3 >"$TMP" 2>&1
else
  python3 "$ROOT/review.py" --max-reviews 3 >"$TMP" 2>&1
fi
rc=$?
if [ "$rc" -ne 0 ] || grep -q '^Reviews written:' "$TMP" || grep -qE '^\s*!|^warning:' "$TMP"; then
  { echo "===== run start ====="; cat "$TMP"; echo "===== run end (exit $rc) ====="; } \
    | while IFS= read -r line; do printf '%s %s\n' "$(date -Iseconds)" "$line"; done >> "$LOG"
fi
