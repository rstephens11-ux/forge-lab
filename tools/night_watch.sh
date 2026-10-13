#!/usr/bin/env bash
# night_watch.sh — exit (and notify) the moment the overnight batch is OVER, one way or the other.
#
# night_report.py waits for the briefs to be *done* and then fetches; this watchdog is for the
# other ending: the runner dying, the service going inactive, or the queue draining without the
# briefs ever finishing. Without it, a silent failure would look like a 9-hour wait.
set -u
HOST="${FORGE_SSH:-ryan@forge}"
DATA="${FORGE_STUDIO_DATA:-/home/ryan/forge-studio-data}"
POLL="${NIGHT_WATCH_POLL_S:-120}"

while true; do
  q=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "ls -1 $DATA/overnight/queue/*.json 2>/dev/null | wc -l" 2>/dev/null)
  r=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "ls -1 $DATA/overnight/running/*.json 2>/dev/null | wc -l" 2>/dev/null)
  act=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "systemctl is-active forge-studio-runner.service" 2>/dev/null)
  d=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "ls -1d $DATA/overnight/done/*/ 2>/dev/null | wc -l" 2>/dev/null)

  if [ "${act:-x}" != "active" ]; then
    echo "OVERNIGHT ENDED: runner service is '${act:-unreachable}' (queue=$q running=$r done=$d)"
    exit 20
  fi
  if [ "${q:-x}" = "0" ] && [ "${r:-x}" = "0" ]; then
    echo "OVERNIGHT ENDED: nothing queued and nothing running (done dirs=$d)"
    exit 0
  fi
  sleep "$POLL"
done