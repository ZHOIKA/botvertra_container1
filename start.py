#!/usr/bin/env python3
import os
import shutil
import socket
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
TOR_TEST_BOT = "bot-01"
TOR_TEST_PORT = 19050
tor_process = None
tor_socks_url = ""

def wait_port(host, port, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False

configured_tor = os.getenv("TOR_TEST_SOCKS_URL", "").strip()
if configured_tor:
    tor_socks_url = configured_tor
    print(f"[tor-test] {TOR_TEST_BOT} usando endpoint SOCKS configurado", flush=True)
else:
    tor_bin = shutil.which("tor")
    if tor_bin:
        tor_data = BASE_DIR / "tor-data"
        tor_data.mkdir(exist_ok=True)
        tor_log = open(LOG_DIR / "tor-test.log", "ab", buffering=0)
        tor_process = subprocess.Popen(
            [
                tor_bin,
                "--SocksPort", f"127.0.0.1:{TOR_TEST_PORT}",
                "--DataDirectory", str(tor_data),
                "--Log", "notice stdout",
            ],
            cwd=str(BASE_DIR),
            stdout=tor_log,
            stderr=subprocess.STDOUT,
        )
        if wait_port("127.0.0.1", TOR_TEST_PORT):
            tor_socks_url = f"socks5h://127.0.0.1:{TOR_TEST_PORT}"
            print(f"[tor-test] {TOR_TEST_BOT} Tor local ativo em {TOR_TEST_PORT}", flush=True)
        else:
            print("[tor-test] binario Tor encontrado, mas SOCKS nao ficou disponivel", flush=True)
    else:
        print("[tor-test] binario Tor nao encontrado na imagem da Vertra", flush=True)

print("[manager] build=container1-tor-test-v1", flush=True)

for i in range(1, 21):
    bot_id = f"bot-{i:02d}"
    env = os.environ.copy()
    env["BOT_ID"] = bot_id

    if bot_id == TOR_TEST_BOT and tor_socks_url:
        env["TOR_SOCKS_URL"] = tor_socks_url
        env["TOR_ISOLATION_ID"] = "container1-bot-01-test"
    else:
        env.pop("TOR_SOCKS_URL", None)
        env.pop("TOR_ISOLATION_ID", None)

    proxy_key = f"BOT_PROXY_{i:02d}"
    bot_proxy = os.getenv(proxy_key, "").strip()
    if bot_proxy:
        env["BOT_PROXY"] = bot_proxy
        env["HTTP_PROXY"] = bot_proxy
        env["HTTPS_PROXY"] = bot_proxy
        env["http_proxy"] = bot_proxy
        env["https_proxy"] = bot_proxy
    else:
        env.pop("BOT_PROXY", None)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(key, None)

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
