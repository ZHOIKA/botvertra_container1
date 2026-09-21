#!/usr/bin/env python3
import asyncio
import json
import os
import platform
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path

BOT_ID = os.getenv("BOT_ID", "bot-unknown")
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
CMD_DIR = BASE_DIR / "commands"

for d in (STATE_DIR, LOG_DIR, CMD_DIR):
    d.mkdir(exist_ok=True)

STARTED_AT = time.time()
WORKER_BUILD = "sync-v4-ipcheck"

ALLOWED_COMMANDS = {
    "ping", "status", "uptime", "hostname",
    "disk", "memory", "echo", "logs", "internet", "public_ip",
}

def read_mem():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            data = {}
            for line in f:
                key, value = line.split(":", 1)
                data[key] = value.strip()
            return {
                "MemTotal": data.get("MemTotal"),
                "MemAvailable": data.get("MemAvailable"),
            }
    except Exception as e:
        return {"error": str(e)}

def check_google_internet():
    target = "www.google.com"
    url = "https://www.google.com/generate_204"
    started = time.perf_counter()

    try:
        resolved_ip = socket.gethostbyname(target)
    except Exception as exc:
        return {
            "ok": False,
            "bot": BOT_ID,
            "internet": False,
            "target": target,
            "stage": "dns",
            "error": str(exc),
        }

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "BotVertra-Connectivity/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            status = int(response.status)

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": status in (200, 204),
            "bot": BOT_ID,
            "internet": status in (200, 204),
            "target": target,
            "resolved_ip": resolved_ip,
            "http_status": status,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": False,
            "bot": BOT_ID,
            "internet": False,
            "target": target,
            "resolved_ip": resolved_ip,
            "stage": "https",
            "latency_ms": latency_ms,
            "error": str(exc),
        }

def get_public_ip():
    url = "https://api.ipify.org?format=json"
    started = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "BotVertra-IPCheck/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        ip = str(payload.get("ip", "")).strip()
        return {
            "ok": bool(ip),
            "bot": BOT_ID,
            "public_ip": ip or None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        return {
            "ok": False,
            "bot": BOT_ID,
            "public_ip": None,
            "error": str(exc),
        }

def tail_logs(lines=40):
    lines = max(1, min(int(lines), 200))
    result = []
    for path in (LOG_DIR / f"{BOT_ID}.log", LOG_DIR / f"{BOT_ID}.stdout.log"):
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            result.append({"file": path.name, "lines": content[-lines:]})
    return result

def execute_command(payload: dict):
    cmd = str(payload.get("command", "")).strip().lower()
    args = payload.get("args", [])

    if cmd not in ALLOWED_COMMANDS:
        return {
            "ok": False,
            "bot": BOT_ID,
            "error": f"command_not_allowed: {cmd}",
            "allowed": sorted(ALLOWED_COMMANDS),
        }

    if cmd == "ping":
        return {"ok": True, "bot": BOT_ID, "result": "pong"}

    if cmd == "status":
        return {
            "ok": True,
            "bot": BOT_ID,
            "status": "online",
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "worker_build": WORKER_BUILD,
            "features": sorted(ALLOWED_COMMANDS),
        }

    if cmd == "uptime":
        return {
            "ok": True,
            "bot": BOT_ID,
            "uptime_seconds": round(time.time() - STARTED_AT, 2),
        }

    if cmd == "hostname":
        return {"ok": True, "bot": BOT_ID, "hostname": socket.gethostname()}

    if cmd == "disk":
        total, used, free = shutil.disk_usage("/")
        return {
            "ok": True,
            "bot": BOT_ID,
            "disk": {
                "total_mb": total // 1024 // 1024,
                "used_mb": used // 1024 // 1024,
                "free_mb": free // 1024 // 1024,
            },
        }

    if cmd == "memory":
        return {"ok": True, "bot": BOT_ID, "memory": read_mem()}

    if cmd == "internet":
        return check_google_internet()

    if cmd == "public_ip":
        return get_public_ip()

    if cmd == "echo":
        return {
            "ok": True,
            "bot": BOT_ID,
            "result": " ".join(str(x) for x in args)[:500],
        }

    if cmd == "logs":
        amount = args[0] if args else 40
        try:
            amount = int(amount)
        except Exception:
            amount = 40
        return {"ok": True, "bot": BOT_ID, "logs": tail_logs(amount)}

    return {"ok": False, "bot": BOT_ID, "error": "unknown"}

async def process_inbox():
    inbox = CMD_DIR / f"{BOT_ID}.json"
    outbox = CMD_DIR / f"{BOT_ID}.out.json"

    while True:
        if inbox.exists():
            try:
                payload = json.loads(inbox.read_text(encoding="utf-8"))
                result = execute_command(payload)
                outbox.write_text(json.dumps(result, indent=2), encoding="utf-8")
                inbox.unlink(missing_ok=True)

                with open(LOG_DIR / f"{BOT_ID}.log", "a", encoding="utf-8") as log:
                    log.write(json.dumps({
                        "ts": time.time(),
                        "input": payload,
                        "output": result
                    }) + "\n")

            except Exception as e:
                err = {"ok": False, "bot": BOT_ID, "error": str(e)}
                outbox.write_text(json.dumps(err, indent=2), encoding="utf-8")
                inbox.unlink(missing_ok=True)

        await asyncio.sleep(0.25)

async def main():
    state = {
        "bot": BOT_ID,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    (STATE_DIR / f"{BOT_ID}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[{BOT_ID}] online pid={os.getpid()}", flush=True)
    try:
        await process_inbox()
    finally:
        (STATE_DIR / f"{BOT_ID}.json").unlink(missing_ok=True)
        print(f"[{BOT_ID}] offline", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
