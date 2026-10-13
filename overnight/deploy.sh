#!/usr/bin/env bash
# overnight/deploy.sh — ship the overnight runner to `forge` and (re)start its service.
#
# Run FROM THE MAC, from anywhere:
#
#     cd <worktree>/overnight && ./deploy.sh
#     ./deploy.sh --character bighead                      # sync only this character
#     ./deploy.sh --characters-src ~/forge-lab-work/testdata-charpack/characters
#
# Idempotent: re-running rsyncs the code, refreshes the characters and restarts the unit.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"          # .../overnight
REPO="$(dirname "$HERE")"                       # the worktree root

HOST="${FORGE_SSH:-ryan@forge}"
REMOTE_CODE="${FORGE_STUDIO_CODE:-/home/ryan/forge-studio}"
REMOTE_DATA="${FORGE_STUDIO_DATA:-/home/ryan/forge-studio-data}"
CHAR_SRC="${CHARACTERS_SRC:-$HOME/forge-lab/characters}"
UNIT_NAME="forge-studio-runner.service"

# --- args -------------------------------------------------------------------
CHARACTERS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --characters-src) CHAR_SRC="$2"; shift 2 ;;
    --character)      CHARACTERS+=("$2"); shift 2 ;;
    --host)           HOST="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
CHAR_SRC="${CHAR_SRC/#\~/$HOME}"

echo "== deploy =="
echo "  host       : $HOST"
echo "  code       : $REPO  ->  $HOST:$REMOTE_CODE"
echo "  data       : $HOST:$REMOTE_DATA"
echo "  characters : $CHAR_SRC  (${CHARACTERS[*]:-all})"

# --- 1. remote dirs ---------------------------------------------------------
ssh "$HOST" "mkdir -p '$REMOTE_CODE/audio' '$REMOTE_CODE/overnight' \
             '$REMOTE_DATA/characters' '$REMOTE_DATA/overnight/queue' \
             '$REMOTE_DATA/overnight/running' '$REMOTE_DATA/overnight/done'"

# --- 2. code (Tk-free modules the runner needs; forge_lab.py is NOT edited) --
CODE_FILES=(forge_lab.py forge_core.py studio_pipeline.py charpack.py director.py)
for f in "${CODE_FILES[@]}"; do
  [ -f "$REPO/$f" ] || { echo "missing $REPO/$f" >&2; exit 1; }
done
rsync -az "${CODE_FILES[@]/#/$REPO/}" "$HOST:$REMOTE_CODE/"
# audio/ (client.py + the rig_*.py scripts it deploys) and overnight/ (runner + client)
rsync -az --delete --include='*/' --include='*.py' --exclude='*' \
      "$REPO/audio/" "$HOST:$REMOTE_CODE/audio/"
rsync -az --delete --include='*/' --include='*.py' --include='*.service' --include='*.sh' \
      --exclude='__pycache__' --exclude='*' \
      "$REPO/overnight/" "$HOST:$REMOTE_CODE/overnight/"

# --- 3. characters (json, still.png, sheet/, bible.json, voice.*) -----------
if [ -d "$CHAR_SRC" ]; then
  if [ "${#CHARACTERS[@]}" -eq 0 ]; then
    rsync -az "$CHAR_SRC/" "$HOST:$REMOTE_DATA/characters/"
  else
    for slug in "${CHARACTERS[@]}"; do
      [ -d "$CHAR_SRC/$slug" ] || { echo "no character '$slug' in $CHAR_SRC" >&2; exit 1; }
      ssh "$HOST" "mkdir -p '$REMOTE_DATA/characters/$slug'"
      rsync -az "$CHAR_SRC/$slug/" "$HOST:$REMOTE_DATA/characters/$slug/"
    done
  fi
else
  echo "!! character source '$CHAR_SRC' does not exist — skipping character sync" >&2
fi

# --- 4. install + (re)start the unit ---------------------------------------
ssh "$HOST" "sudo cp '$REMOTE_CODE/overnight/$UNIT_NAME' /etc/systemd/system/$UNIT_NAME && \
             sudo systemctl daemon-reload && \
             sudo systemctl enable '$UNIT_NAME' >/dev/null 2>&1 && \
             sudo systemctl restart '$UNIT_NAME'"

# --- 5. status --------------------------------------------------------------
echo "== status =="
ssh "$HOST" "systemctl status '$UNIT_NAME' --no-pager -l | head -n 12; \
             echo '--- heartbeat ---'; \
             cat '$REMOTE_DATA/overnight/status.json' 2>/dev/null || echo '(none yet)'; \
             echo; echo '--- tkinter check (must be absent) ---'; \
             cd '$REMOTE_CODE' && FORGE_LAB_DATA='$REMOTE_DATA' FORGE_URL='http://127.0.0.1:8188' \
             /usr/bin/python3 -c 'import sys; import forge_core; print(\"tkinter imported:\", \"tkinter\" in sys.modules)' \
             2>&1 || true"
