#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -ne 1 ]]; then
  echo "Uso: ./read.sh <bot-01>"
  exit 1
fi

FILE="commands/$1.out.json"
if [[ -f "$FILE" ]]; then
  cat "$FILE"
else
  echo "Sem resposta ainda para $1"
fi
