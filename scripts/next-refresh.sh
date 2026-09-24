#!/usr/bin/env bash
# Queue the next intraday run of update.yml at the following :07 or :37 slot, but only
# while that slot falls in the US session (9:30-16:45 New York, weekdays; the last slot
# lands after 16:15 and builds the official close). NOW overrides the clock and DRY_RUN=1
# prints the decision without sleeping or dispatching, for testing.
set -euo pipefail
now=${NOW:-$(date -u +%s)}
m=$(( 10#$(date -u -d "@$now" +%M) ))
if   (( m < 2 ));  then add=$(( 7 - m ))       # at least 5 minutes out
elif (( m < 32 )); then add=$(( 37 - m ))
else                    add=$(( 67 - m )); fi
slot=$(( now - 10#$(date -u -d "@$now" +%S) + add * 60 ))
ny() { TZ=America/New_York date -d "@$slot" "$@"; }
# A run just before the open (the 9:07 cron) waits for the 9:37 slot rather than ending.
if (( 10#$(ny +%H) * 60 + 10#$(ny +%M) < 570 && slot + 1800 - now < 2400 )); then slot=$(( slot + 1800 )); fi
dow=$(ny +%u)
hm=$(( 10#$(ny +%H) * 60 + 10#$(ny +%M) ))
if (( dow > 5 || hm < 570 || hm > 1005 )); then
  echo "next slot $(ny '+%a %H:%M') ET is outside the session - chain ends"; exit 0
fi
if [[ -n "${DRY_RUN:-}" ]]; then echo "would queue the $(ny '+%a %H:%M') ET refresh"; exit 0; fi
sleep $(( slot - now + RANDOM % 60 ))
# A push during the session starts a second chain; if the other one already queued this
# slot's run, stand down so the two merge back into one.
recent=$(gh run list -w update.yml -e workflow_dispatch -L 5 --json createdAt \
  --jq "[.[] | select((.createdAt | fromdateiso8601) > $slot - 600)] | length")
if (( recent > 0 )); then echo "another chain already queued this slot"; exit 0; fi
gh workflow run update.yml --ref main
echo "queued the $(ny +%H:%M) ET refresh"
