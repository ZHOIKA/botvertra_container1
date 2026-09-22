#!/usr/bin/env python3
"""Manager do container 1 - 20 bots, cada um com IP publico proprio.

Roteamento por bot (ordem de prioridade):
  1. BOT_PROXY_<NN>  -> proxy dedicado (garante IP unico e estavel)
  2. Tor compartilhado -> circuito isolado por bot (IsolateSOCKSAuth)
  3. direto          -> IP da propria VPS (so quando nao ha 1 nem 2)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from tor_manager import start_shared_tor

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
PID_DIR = BASE_DIR / "pids"
STATE_DIR = BASE_DIR / "state"
CMD_DIR = BASE_DIR / "commands"

for directory in (LOG_DIR, PID_DIR, STATE_DIR, CMD_DIR):
    directory.mkdir(exist_ok=True)

CONTAINER_NAME = os.getenv("CONTAINER_NAME", "container1").strip()
BOT_COUNT = int(os.getenv("BOT_COUNT", "20"))
TOR_SOCKS_PORT = int(os.getenv("TOR_SOCKS_PORT", "19050"))

processes = []
print(f"[manager] build={CONTAINER_NAME}-tor-isolation-v1", flush=True)

# --- Tor compartilhado (um por container, isolamento por bot) --------------- #
tor = start_shared_tor(BASE_DIR, STATE_DIR, LOG_DIR, TOR_SOCKS_PORT, CONTAINER_NAME)
tor_socks_url = tor["socks_url"]
tor_process = tor["process"]
if tor_socks_url:
    print(f"[tor] SOCKS pronto: {tor_socks_url}", flush=True)
else:
    print("[tor] sem SOCKS; bots sem proxy dedicado usarao o IP da VPS", flush=True)


def spawn_bots():
    procs = []
    for i in range(1, BOT_COUNT + 1):
        bot_id = f"bot-{i:02d}"
        env = os.environ.copy()
        env["BOT_ID"] = bot_id

        # Nunca herdar proxy/isolation do ambiente do manager.
        for key in ("BOT_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                    "https_proxy", "TOR_SOCKS_URL", "TOR_ISOLATION_ID"):
            env.pop(key, None)

        bot_proxy = os.getenv(f"BOT_PROXY_{i:02d}", "").strip()

        if bot_proxy:
            env["BOT_PROXY"] = bot_proxy
            env["HTTP_PROXY"] = bot_proxy
            env["HTTPS_PROXY"] = bot_proxy
            env["http_proxy"] = bot_proxy
            env["https_proxy"] = bot_proxy
            route = "proxy"
        elif tor_socks_url:
            env["TOR_SOCKS_URL"] = tor_socks_url
            env["TOR_ISOLATION_ID"] = f"{CONTAINER_NAME}-{bot_id}"
            route = "tor"
        else:
            route = "direct"

        log_file = open(LOG_DIR / f"{bot_id}.stdout.log", "ab", buffering=0)
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "bot.py")],
            cwd=str(BASE_DIR),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        (PID_DIR / f"{bot_id}.pid").write_text(str(proc.pid), encoding="utf-8")
        procs.append((bot_id, proc, log_file))
        print(f"[{bot_id}] iniciado pid={proc.pid} route={route}", flush=True)
    return procs


processes = spawn_bots()

bridge = subprocess.Popen(
    [sys.executable, str(BASE_DIR / "remote_bridge.py")],
    cwd=str(BASE_DIR),
    env=os.environ.copy(),
)

print(f"[manager] {len(processes)} bots iniciados", flush=True)
print(f"[manager] bridge externo iniciado pid={bridge.pid}", flush=True)

try:
    while True:
        alive = sum(1 for _, proc, _ in processes if proc.poll() is None)
        if alive == 0:
            raise SystemExit("Todos os bots foram encerrados")

        if bridge.poll() is not None:
            print(f"[manager] bridge caiu ({bridge.returncode}); reiniciando", flush=True)
            bridge = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "remote_bridge.py")],
                cwd=str(BASE_DIR),
                env=os.environ.copy(),
            )

        time.sleep(5)

except KeyboardInterrupt:
    print("[manager] encerrando...", flush=True)

finally:
    if bridge.poll() is None:
        bridge.terminate()

    if tor_process is not None and tor_process.poll() is None:
        tor_process.terminate()

    for _, proc, log_file in processes:
        if proc.poll() is None:
            proc.terminate()
        log_file.close()

    for _, proc, _ in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("[manager] finalizado", flush=True)
