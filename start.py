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
TOR_STATE_FILE = STATE_DIR / "tor-test.json"
tor_process = None
tor_socks_url = ""

def write_tor_state(stage, ok=False, detail=None, socks_url=None):
    payload = {
        "stage": stage,
        "ok": bool(ok),
        "detail": detail,
        "socks_url": socks_url,
        "updated_at": time.time(),
    }
    TOR_STATE_FILE.write_text(__import__("json").dumps(payload, indent=2), encoding="utf-8")

def wait_port(host, port, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False

def ensure_tor_binary():
    existing = shutil.which("tor")
    if existing:
        write_tor_state("tor_binary_found", True, existing)
        return existing

    apt = shutil.which("apt-get")
    apk = shutil.which("apk")
    dnf = shutil.which("dnf")
    yum = shutil.which("yum")
    if not any((apt, apk, dnf, yum)):
        write_tor_state("package_manager_missing", False, "apt-get/apk/dnf/yum nao encontrados")
        print("[tor-test] gerenciador de pacotes nao encontrado", flush=True)
        return None

    install_log_path = LOG_DIR / "tor-install.log"
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        prefix = []
    else:
        sudo = shutil.which("sudo")
        if not sudo:
            write_tor_state("install_permission_denied", False, "sem root e sem sudo")
            print("[tor-test] sem root/sudo para instalar Tor automaticamente", flush=True)
            return None
        prefix = [sudo, "-n"]

    if apt:
        commands = [
            prefix + [apt, "update"],
            prefix + [apt, "install", "-y", "--no-install-recommends", "tor"],
        ]
    elif apk:
        commands = [prefix + [apk, "add", "--no-cache", "tor"]]
    elif dnf:
        commands = [prefix + [dnf, "install", "-y", "tor"]]
    else:
        commands = [prefix + [yum, "install", "-y", "tor"]]

    try:
        with open(install_log_path, "ab", buffering=0) as install_log:
            for cmd in commands:
                result = subprocess.run(
                    cmd,
                    cwd=str(BASE_DIR),
                    env=env,
                    stdout=install_log,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    check=False,
                )
                if result.returncode != 0:
                    write_tor_state(
                        "install_failed",
                        False,
                        f"comando={' '.join(cmd)} rc={result.returncode}; veja logs/tor-install.log",
                    )
                    print(
                        f"[tor-test] instalacao do Tor falhou rc={result.returncode}; veja logs/tor-install.log",
                        flush=True,
                    )
                    return None
    except Exception as exc:
        write_tor_state("install_exception", False, str(exc))
        print(f"[tor-test] erro instalando Tor: {exc}", flush=True)
        return None

    installed = shutil.which("tor")
    if installed:
        write_tor_state("tor_installed", True, installed)
        print("[tor-test] Tor instalado automaticamente", flush=True)
    else:
        write_tor_state("tor_binary_missing_after_install", False, "pacote instalou mas binario nao apareceu no PATH")
        print("[tor-test] pacote instalado, mas binario tor nao apareceu no PATH", flush=True)
    return installed

configured_tor = os.getenv("TOR_TEST_SOCKS_URL", "").strip()
if configured_tor:
    tor_socks_url = configured_tor
    write_tor_state("external_socks_configured", True, "TOR_TEST_SOCKS_URL", tor_socks_url)
    print(f"[tor-test] {TOR_TEST_BOT} usando endpoint SOCKS configurado", flush=True)
else:
    tor_bin = ensure_tor_binary()
    if tor_bin:
        tor_data = BASE_DIR / "tor-data"
        tor_data.mkdir(exist_ok=True)
        tor_log = open(LOG_DIR / "tor-test.log", "ab", buffering=0)
        tor_process = subprocess.Popen(
            [
                tor_bin,
                "--SocksPort", f"127.0.0.1:{TOR_TEST_PORT} IsolateSOCKSAuth",
                "--DataDirectory", str(tor_data),
                "--Log", "notice stdout",
            ],
            cwd=str(BASE_DIR),
            stdout=tor_log,
            stderr=subprocess.STDOUT,
        )
        if wait_port("127.0.0.1", TOR_TEST_PORT):
            tor_socks_url = f"socks5h://127.0.0.1:{TOR_TEST_PORT}"
            write_tor_state("tor_ready", True, "SOCKS local ativo", tor_socks_url)
            print(f"[tor-test] {TOR_TEST_BOT} Tor local ativo em {TOR_TEST_PORT}", flush=True)
        else:
            write_tor_state("tor_socks_unavailable", False, "binario iniciou mas porta SOCKS nao abriu")
            print("[tor-test] binario Tor encontrado, mas SOCKS nao ficou disponivel", flush=True)
    else:
        if not TOR_STATE_FILE.exists():
            write_tor_state("tor_binary_unavailable", False, "nao foi possivel localizar/instalar o binario Tor")
        print("[tor-test] binario Tor nao encontrado na imagem da Vertra", flush=True)

print("[manager] build=container1-tor-test-v3", flush=True)

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
