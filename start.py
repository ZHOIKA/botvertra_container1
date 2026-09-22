#!/usr/bin/env python3
"""Manager do container - 20 bots com IP proprio e rotacao automatica.

Roteamento por bot:
  1. BOT_PROXY_<NN> (+ _ALTk) -> proxy dedicado (pool rotativo)
  2. Tor compartilhado        -> circuito isolado por bot (IsolateSOCKSAuth)
  3. direto                   -> IP da VPS

Rotacao automatica (ip_rotator):
  * bots em local_timeout / bot_timeout -> reconecta por rota diferente
  * bots com IP publico duplicado       -> reconecta por rota diferente
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import ip_rotator
from ip_rotator import all_bots
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
IP_AUDIT_INTERVAL = int(os.getenv("IP_AUDIT_INTERVAL", "90"))

os.environ["CONTAINER_NAME"] = CONTAINER_NAME
os.environ["BOT_COUNT"] = str(BOT_COUNT)

print(f"[manager] build={CONTAINER_NAME}-auto-rotate-v1", flush=True)

# --- Tor compartilhado (um por container, isolamento por bot) --------------- #
tor = start_shared_tor(BASE_DIR, STATE_DIR, LOG_DIR, TOR_SOCKS_PORT, CONTAINER_NAME)
tor_process = tor["process"]
if tor["socks_url"]:
    os.environ["TOR_SOCKS_URL"] = tor["socks_url"]
    print(f"[tor] SOCKS pronto: {tor['socks_url']}", flush=True)
else:
    print("[tor] sem SOCKS; bots sem proxy dedicado usarao o IP da VPS", flush=True)

bots = {}


def _clean_env(bot_env):
    for key in ("BOT_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                "https_proxy", "TOR_SOCKS_URL", "TOR_ISOLATION_ID"):
        bot_env.pop(key, None)
    return bot_env


def launch(bot, gen):
    env = _clean_env(os.environ.copy())
    env["BOT_ID"] = bot
    route = ip_rotator.resolve_route(bot, gen)
    env.update(route["env"])

    log_file = open(LOG_DIR / f"{bot}.stdout.log", "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "bot.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    (PID_DIR / f"{bot}.pid").write_text(str(proc.pid), encoding="utf-8")
    return {"proc": proc, "log": log_file, "gen": gen,
            "mode": route["mode"], "target": route["target"]}


def stop(bot):
    rec = bots.pop(bot, None)
    if not rec:
        return
    proc = rec["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        rec["log"].close()
    except OSError:
        pass


def relaunch(bot, reason=None):
    gen = bots[bot]["gen"] if bot in bots else ip_rotator.generation(bot)
    if reason:
        gen = ip_rotator.bump_generation(bot, reason)
    stop(bot)
    rec = launch(bot, gen)
    bots[bot] = rec
    label = reason or "recovery"
    print(f"[{bot}] iniciado pid={rec['proc'].pid} gen={gen} "
          f"route={rec['mode']}:{rec['target']} motivo={label}", flush=True)


for _bot in all_bots():
    relaunch(_bot, None)

bridge = subprocess.Popen(
    [sys.executable, str(BASE_DIR / "remote_bridge.py")],
    cwd=str(BASE_DIR),
    env=os.environ.copy(),
)

print(f"[manager] {len(bots)} bots iniciados", flush=True)
print(f"[manager] bridge externo iniciado pid={bridge.pid}", flush=True)

# --- auditoria de IP (duplicados) + rotacao por timeout --------------------- #
if IP_AUDIT_INTERVAL > 0:
    threading.Thread(
        target=ip_rotator.audit_loop, args=(IP_AUDIT_INTERVAL,), daemon=True
    ).start()
    print(f"[manager] auditoria de IP ativa (intervalo={IP_AUDIT_INTERVAL}s)", flush=True)
else:
    print("[manager] auditoria de IP desativada (IP_AUDIT_INTERVAL=0)", flush=True)

try:
    while True:
        for bot in list(bots):
            reason = ip_rotator.consume_rotation(bot)
            if reason:
                relaunch(bot, reason)
            elif bots[bot]["proc"].poll() is not None:
                relaunch(bot, None)

        if not any(rec["proc"].poll() is None for rec in bots.values()):
            raise SystemExit("Todos os bots foram encerrados")

        if bridge.poll() is not None:
            print(f"[manager] bridge caiu ({bridge.returncode}); reiniciando", flush=True)
            bridge = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "remote_bridge.py")],
                cwd=str(BASE_DIR),
                env=os.environ.copy(),
            )

        time.sleep(2)

except KeyboardInterrupt:
    print("[manager] encerrando...", flush=True)

finally:
    if bridge.poll() is None:
        bridge.terminate()

    if tor_process is not None and tor_process.poll() is None:
        tor_process.terminate()

    for bot in list(bots):
        stop(bot)

    print("[manager] finalizado", flush=True)
