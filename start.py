#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
PID_DIR = BASE_DIR / "pids"
STATE_DIR = BASE_DIR / "state"
CMD_DIR = BASE_DIR / "commands"

for directory in (LOG_DIR, PID_DIR, STATE_DIR, CMD_DIR):
    directory.mkdir(exist_ok=True)

processes = []
print("[manager] build=bridge-v3", flush=True)

for i in range(1, 21):
    bot_id = f"bot-{i:02d}"
    env = os.environ.copy()
    env["BOT_ID"] = bot_id

    log_path = LOG_DIR / f"{bot_id}.stdout.log"
    log_file = open(log_path, "ab", buffering=0)

    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "bot.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    (PID_DIR / f"{bot_id}.pid").write_text(str(proc.pid), encoding="utf-8")
    processes.append((bot_id, proc, log_file))
    print(f"[{bot_id}] iniciado pid={proc.pid}", flush=True)

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
