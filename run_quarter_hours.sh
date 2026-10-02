#!/usr/bin/env bash
# Started once an hour by the scheduled Routine: runs both TSLA strategies
# now and every 15 minutes after, four times in all, then exits.
# The scripts themselves do nothing while the market is closed.
cd "$(dirname "$0")"
for i in 1 2 3 4; do
  echo "=== $(TZ=America/New_York date '+%Y-%m-%d %H:%M ET') run $i/4 ==="
  python3 trailing_monitor.py 2>&1
  python3 wheel.py 2>&1
  [ "$i" -lt 4 ] && sleep 900
done
