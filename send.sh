#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -lt 2 ]]; then
  echo "Uso: ./send.sh <bot-01|all> <comando> [args...]"
  echo "Comandos: ping status uptime hostname disk memory echo stop"
  exit 1
fi

TARGET="$1"
COMMAND="$2"
shift 2

case "$COMMAND" in
  ping|status|uptime|hostname|disk|memory|echo|stop) ;;
  *)
    echo "Comando não permitido: $COMMAND"
    exit 2
    ;;
esac

ARGS_JSON="$(python3 - "$@" <<'PY'
import json, sys
print(json.dumps(sys.argv[1:]))
PY
)"

send_one() {
  local bot="$1"
  python3 - "$bot" "$COMMAND" "$ARGS_JSON" <<'PY'
import json, pathlib, sys
bot, cmd, args_json = sys.argv[1], sys.argv[2], sys.argv[3]
pathlib.Path("commands").mkdir(exist_ok=True)
pathlib.Path(f"commands/{bot}.json").write_text(
    json.dumps({"command": cmd, "args": json.loads(args_json)}, indent=2),
    encoding="utf-8"
)
PY
  echo "Enviado para $bot: $COMMAND"
}

if [[ "$TARGET" == "all" ]]; then
  for i in $(seq -w 1 20); do
    send_one "bot-$i"
  done
else
  send_one "$TARGET"
fi
