#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

for i in $(seq -w 1 20); do
  BOT_ID="bot-$i"
  PIDFILE="pids/$BOT_ID.pid"

  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "$BOT_ID ONLINE pid=$(cat "$PIDFILE")"
  else
    echo "$BOT_ID OFFLINE"
  fi
done
