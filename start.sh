#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs pids state commands

for i in $(seq -w 1 20); do
  BOT_ID="bot-$i"
  PIDFILE="pids/$BOT_ID.pid"

  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[$BOT_ID] já está rodando"
    continue
  fi

  BOT_ID="$BOT_ID" nohup python3 bot.py >> "logs/$BOT_ID.stdout.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "[$BOT_ID] iniciado PID $(cat "$PIDFILE")"
done
