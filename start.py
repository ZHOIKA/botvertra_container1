#!/usr/bin/env python3
import os
import urllib.request
import tarfile
import platform
import hashlib
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
TOR_BUNDLE_VERSION = "15.0.23"
TOR_BUNDLE_NAME = f"tor-expert-bundle-linux-x86_64-{TOR_BUNDLE_VERSION}.tar.gz"
TOR_BUNDLE_SHA256 = "08d49de27f542b8f73e2014e064d8320562b5d20019c03d4725c5a5249d97985"
TOR_BUNDLE_URLS = [
    f"https://dist.torproject.org/torbrowser/{TOR_BUNDLE_VERSION}/{TOR_BUNDLE_NAME}",
    f"https://archive.torproject.org/tor-package-archive/torbrowser/{TOR_BUNDLE_VERSION}/{TOR_BUNDLE_NAME}",
]
TOR_VENDOR_DIR = BASE_DIR / ".local" / "tor-expert"
TOR_ARCHIVE_PATH = STATE_DIR / TOR_BUNDLE_NAME
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

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def find_bundled_tor(root):
    preferred = root / "tor" / "tor"
    candidates = [preferred] if preferred.exists() else []
    candidates += [
        path for path in root.rglob("tor")
        if path.is_file() and path not in candidates
    ]
    for candidate in candidates:
        try:
            candidate.chmod(candidate.stat().st_mode | 0o111)
        except OSError:
            pass
        if os.access(candidate, os.X_OK):
            return candidate
    return None

def tor_runtime_env(tor_bin):
    env = os.environ.copy()
    # O Expert Bundle guarda as bibliotecas carregaveis ao lado do binario.
    # Nao inclua a pasta debug/: ela contem arquivos de simbolos com nomes
    # iguais aos .so reais e o dynamic loader pode tentar carrega-los.
    libdir = str(tor_bin.parent)
    old = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = libdir + (":" + old if old else "")
    env["HOME"] = str(BASE_DIR)
    return env

def safe_extract_tar(archive_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tf:
        try:
            tf.extractall(destination, filter="data")
        except TypeError:
            root = destination.resolve()
            for member in tf.getmembers():
                target = (destination / member.name).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"unsafe_archive_member: {member.name}")
            tf.extractall(destination)

def download_tor_bundle():
    if TOR_ARCHIVE_PATH.exists():
        current = sha256_file(TOR_ARCHIVE_PATH)
        if current == TOR_BUNDLE_SHA256:
            write_tor_state("bundle_cached", True, f"sha256={current}")
            return True
        TOR_ARCHIVE_PATH.unlink(missing_ok=True)

    tmp_path = TOR_ARCHIVE_PATH.with_suffix(TOR_ARCHIVE_PATH.suffix + ".part")
    tmp_path.unlink(missing_ok=True)
    errors = []

    for url in TOR_BUNDLE_URLS:
        try:
            write_tor_state("bundle_downloading", False, url)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "BotVertra-TorTest/1.0"},
                method="GET",
            )
            total = 0
            with urllib.request.urlopen(request, timeout=90) as response, open(tmp_path, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise RuntimeError("bundle_too_large")
                    out.write(chunk)

            actual = sha256_file(tmp_path)
            if actual != TOR_BUNDLE_SHA256:
                raise RuntimeError(
                    f"sha256_mismatch expected={TOR_BUNDLE_SHA256} actual={actual}"
                )
            os.replace(tmp_path, TOR_ARCHIVE_PATH)
            write_tor_state("bundle_verified", True, f"{url} sha256={actual}")
            return True
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            tmp_path.unlink(missing_ok=True)

    write_tor_state("bundle_download_failed", False, " | ".join(errors))
    return False

def ensure_tor_binary():
    system_tor = shutil.which("tor")
    if system_tor:
        write_tor_state("tor_binary_found", True, system_tor)
        return system_tor

    machine = platform.machine().lower()
    if machine not in ("x86_64", "amd64"):
        write_tor_state("unsupported_arch", False, f"arquitetura={machine}")
        return None

    bundled = find_bundled_tor(TOR_VENDOR_DIR)
    if bundled:
        write_tor_state("bundled_tor_found", True, str(bundled))
        return str(bundled)

    if not download_tor_bundle():
        return None

    try:
        if TOR_VENDOR_DIR.exists():
            shutil.rmtree(TOR_VENDOR_DIR)
        safe_extract_tar(TOR_ARCHIVE_PATH, TOR_VENDOR_DIR)
    except Exception as exc:
        write_tor_state("bundle_extract_failed", False, str(exc))
        return None

    bundled = find_bundled_tor(TOR_VENDOR_DIR)
    if not bundled:
        write_tor_state(
            "bundle_tor_missing",
            False,
            "bundle extraido, mas o executavel tor nao foi localizado",
        )
        return None

    try:
        version_result = subprocess.run(
            [str(bundled), "--version"],
            cwd=str(bundled.parent),
            env=tor_runtime_env(bundled),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
        detail = version_result.stdout.strip().replace("\n", " ")[:500]
        if version_result.returncode != 0:
            write_tor_state(
                "bundled_tor_exec_failed",
                False,
                f"rc={version_result.returncode} {detail}",
            )
            return None
        write_tor_state("bundled_tor_ready", True, detail or str(bundled))
        return str(bundled)
    except Exception as exc:
        write_tor_state("bundled_tor_exec_exception", False, str(exc))
        return None

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
        tor_bin_path = Path(tor_bin)
        tor_env = tor_runtime_env(tor_bin_path) if str(tor_bin_path).startswith(str(TOR_VENDOR_DIR)) else os.environ.copy()
        tor_process = subprocess.Popen(
            [
                tor_bin,
                "--ClientOnly", "1",
                "--SocksPort", f"127.0.0.1:{TOR_TEST_PORT} IsolateSOCKSAuth",
                "--DataDirectory", str(tor_data),
                "--Log", "notice stdout",
            ],
            cwd=str(tor_bin_path.parent),
            env=tor_env,
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

print("[manager] build=container1-tor-userspace-v2", flush=True)

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
