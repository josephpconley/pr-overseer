#!/bin/bash
# Drop cron.log lines older than 7 days and test-refactor.log lines older than 35 days (weekly job: keep
# ~5 runs); delete other log files 7+ days old. DRY=1 to preview.
D=$(cd "$(dirname "$0")" && pwd)/logs; LOG=$D/cron.log; TLOG=$D/test-refactor.log
CUT=$(date -d '7 days ago' -Iseconds)
TCUT=$(date -d '35 days ago' -Iseconds)
if [ "${DRY:-0}" = 1 ]; then
  echo "cron.log lines that would be dropped (< $CUT): $(awk -v c="$CUT" '$1<c' "$LOG" 2>/dev/null | wc -l)"
  echo "test-refactor.log lines that would be dropped (< $TCUT): $(awk -v c="$TCUT" '$1<c' "$TLOG" 2>/dev/null | wc -l)"
  find "$D" -maxdepth 1 -type f \( -name '*.log*' -o -name '*.txt' \) ! -name cron.log ! -name test-refactor.log -mtime +6 -print
  exit 0
fi
[ -f "$LOG" ] && awk -v c="$CUT" '$1>=c' "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
[ -f "$TLOG" ] && awk -v c="$TCUT" '$1>=c' "$TLOG" > "$TLOG.tmp" && mv "$TLOG.tmp" "$TLOG"
find "$D" -maxdepth 1 -type f \( -name '*.log*' -o -name '*.txt' \) ! -name cron.log ! -name test-refactor.log -mtime +6 -delete
