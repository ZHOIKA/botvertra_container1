#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 não encontrado."
  echo "Em Debian/Ubuntu: apt update && apt install -y python3"
  exit 1
fi

python3 - <<'PY'
import sys
print("Python detectado:", sys.version)
if sys.version_info < (3, 10):
    raise SystemExit("Use Python 3.10+; recomendado 3.13.")
PY

chmod +x start.sh stop.sh status.sh send.sh read.sh install.sh
mkdir -p logs pids state commands
echo "Instalação preparada."
echo "Inicie com: ./start.sh"
