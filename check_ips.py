#!/usr/bin/env python3
"""Verifica o IP publico de cada bot deste container e aponta colisoes.

Uso:
    python3 check_ips.py            # checa bot-01..bot-20
    python3 check_ips.py 40         # checa bot-01..bot-40
"""
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CMD_DIR = BASE_DIR / "commands"


def run_on_bot(bot: str, timeout: float = 25.0):
    inbox = CMD_DIR / f"{bot}.json"
    outbox = CMD_DIR / f"{bot}.out.json"

    if not (BASE_DIR / "state" / f"{bot}.json").exists():
        return {"ok": False, "bot": bot, "error": "bot_offline"}

    outbox.unlink(missing_ok=True)
    inbox.write_text(json.dumps({"command": "public_ip", "args": []}), encoding="utf-8")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if outbox.exists():
            try:
                return json.loads(outbox.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
    return {"ok": False, "bot": bot, "error": "timeout"}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    seen = {}
    duplicates = 0

    print(f"{'BOT':<8} {'IP':<18} {'ROTA':<10} OBS")
    print("-" * 60)

    for i in range(1, count + 1):
        bot = f"bot-{i:02d}"
        res = run_on_bot(bot)
        ip = res.get("public_ip")
        route = (res.get("route") or {}).get("mode", "-")

        obs = ""
        if ip:
            if ip in seen:
                obs = f"DUPLICADO (igual a {seen[ip]})"
                duplicates += 1
            else:
                seen[ip] = bot
        else:
            obs = res.get("error", "sem IP")

        print(f"{bot:<8} {str(ip or '-'):<18} {route:<10} {obs}")

    print("-" * 60)
    print(f"IPs unicos: {len(seen)}/{count} | colisoes: {duplicates}")
    if duplicates:
        print("Dica: coloque proxy dedicado nos bots duplicados via BOT_PROXY_<NN>.")


if __name__ == "__main__":
    main()
