#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

for f in pids/*.pid; do
  [[ -e "$f" ]] || continue
  pid="$(cat "$f")"
  name="$(basename "$f" .pid)"

  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    echo "[$name] parado"
  else
    echo "[$name] já estava offline"
  fi

  rm -f "$f"
done
