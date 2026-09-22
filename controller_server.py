#!/usr/bin/env python3
import asyncio
import json
import os
import secrets
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

TOKEN = os.getenv("CONTROLLER_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("CONTROLLER_TOKEN ausente")

app = FastAPI(title="BotVertra Controller")

agents = {}
pending = {}
agent_locks = {}
bot_ip_cache = {}
dashboard_clients = set()
tor_test_state = {
    "container": "container1",
    "bot": "bot-01",
    "checked_at": 0,
    "tor_configured": False,
    "public_ip": None,
    "worker_build": None,
    "error": "aguardando_teste",
}

def remember_public_ip_result(item):
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    ip = result.get("public_ip")
    container_name = item.get("container")
    bot = item.get("bot")
    if item.get("ok") and ip and container_name and bot:
        bot_ip_cache[(container_name, bot)] = {
            "public_ip": str(ip),
            "checked_at": time.time(),
        }

def remember_public_ip_results(items):
    for item in items:
        remember_public_ip_result(item)

async def run_selftest(label):
    targets = []
    for container_name, agent in sorted(agents.items()):
        if agent.get("ws") is None:
            continue
        for bot in sorted(agent.get("bots", set())):
            targets.append((container_name, bot))

    if not targets:
        print(f"[selftest:{label}] nenhum bot conectado para testar", flush=True)
        return

    results = await asyncio.gather(*[
        execute_one(container_name, bot, "ping", [])
        for container_name, bot in targets
    ])

    ok = sum(
        1 for item in results
        if item.get("ok")
        and isinstance(item.get("result"), dict)
        and item["result"].get("result") == "pong"
    )
    failed = len(results) - ok
    print(f"[selftest:{label}] ping concluido • {ok}/{len(results)} OK • {failed} falha(s)", flush=True)

    for item in results:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if not (item.get("ok") and result.get("result") == "pong"):
            print(f"[selftest:{label}][fail] {item}", flush=True)

async def run_internet_selftest():
    targets = []
    for container_name, agent in sorted(agents.items()):
        if agent.get("ws") is None:
            continue
        for bot in sorted(agent.get("bots", set())):
            targets.append((container_name, bot))

    if not targets:
        print("[internet-test] nenhum bot conectado para testar", flush=True)
        return

    results = await asyncio.gather(*[
        execute_one(container_name, bot, "internet", [])
        for container_name, bot in targets
    ])

    ok = 0
    failures = []
    for item in results:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if item.get("ok") and result.get("internet") is True:
            ok += 1
        else:
            failures.append(item)

    print(f"[internet-test] Google HTTPS • {ok}/{len(results)} OK • {len(failures)} falha(s)", flush=True)
    for item in failures:
        print(f"[internet-test][fail] {item}", flush=True)

async def run_public_ip_selftest():
    targets = []
    for container_name, agent in sorted(agents.items()):
        if agent.get("ws") is None:
            continue
        for bot in sorted(agent.get("bots", set())):
            targets.append((container_name, bot))

    if not targets:
        print("[public-ip-test] nenhum bot conectado para testar", flush=True)
        return

    results = await asyncio.gather(*[
        execute_one(container_name, bot, "public_ip", [])
        for container_name, bot in targets
    ])

    remember_public_ip_results(results)
    await broadcast_dashboard()

    ip_to_targets = {}
    failures = []
    for item in results:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        ip = result.get("public_ip")
        if item.get("ok") and ip:
            ip_to_targets.setdefault(ip, []).append(
                f"{item.get('container')}/{item.get('bot')}"
            )
        else:
            failures.append(item)

    print(
        f"[public-ip-test] {len(results) - len(failures)}/{len(results)} OK • "
        f"{len(ip_to_targets)} IP(s) publico(s) unico(s) • {len(failures)} falha(s)",
        flush=True,
    )
    for ip, owners in sorted(ip_to_targets.items()):
        print(
            f"[public-ip-test][ip] {ip} • {len(owners)} bot(s) • " + ", ".join(owners),
            flush=True,
        )
    for item in failures:
        print(f"[public-ip-test][fail] {item}", flush=True)

async def run_single_tor_test():
    global tor_test_state
    container_name = "container1"
    bot = "bot-01"

    status_item = await execute_one(container_name, bot, "status", [])
    ip_item = await execute_one(container_name, bot, "public_ip", [])

    remember_public_ip_result(ip_item)

    status_result = status_item.get("result") if isinstance(status_item.get("result"), dict) else {}
    ip_result = ip_item.get("result") if isinstance(ip_item.get("result"), dict) else {}

    tor_test_state = {
        "container": container_name,
        "bot": bot,
        "checked_at": time.time(),
        "tor_configured": bool(status_result.get("tor_configured")),
        "public_ip": ip_result.get("public_ip"),
        "worker_build": status_result.get("worker_build"),
        "setup": status_result.get("tor_test_state"),
        "error": None if status_item.get("ok") and ip_item.get("ok") else (
            status_item.get("error") or ip_item.get("error") or
            status_result.get("error") or ip_result.get("error") or "test_failed"
        ),
    }
    await broadcast_dashboard()
    print(
        f"[tor-test] {container_name}/{bot} • "
        f"tor={'ON' if tor_test_state['tor_configured'] else 'OFF'} • "
        f"ip={tor_test_state['public_ip']} • "
        f"build={tor_test_state['worker_build']} • "
        f"setup={tor_test_state.get('setup')} • "
        f"error={tor_test_state['error']}",
        flush=True,
    )

async def delayed_tor_test():
    await asyncio.sleep(50)
    await run_single_tor_test()

async def delayed_tor_reconnect_test():
    await asyncio.sleep(12)
    await run_single_tor_test()

async def delayed_selftest():
    await asyncio.sleep(25)
    await run_selftest("25s")
    await asyncio.sleep(40)
    await run_selftest("65s")
    await asyncio.sleep(25)
    await run_internet_selftest()
    await asyncio.sleep(20)
    await run_public_ip_selftest()

async def delayed_public_ip_test():
    await asyncio.sleep(45)
    await run_public_ip_selftest()

async def periodic_ip_cache_refresh():
    # Dá tempo para os bridges reconectarem após deploy/restart.
    await asyncio.sleep(150)
    while True:
        try:
            await run_public_ip_selftest()
        except Exception as exc:
            print(f"[ip-cache] refresh falhou: {exc}", flush=True)
        await asyncio.sleep(120)

@app.on_event("startup")
async def start_selftest():
    asyncio.create_task(delayed_selftest())
    asyncio.create_task(delayed_public_ip_test())
    asyncio.create_task(delayed_tor_test())
    asyncio.create_task(periodic_ip_cache_refresh())
    asyncio.create_task(dashboard_heartbeat())
ALLOWED = {
    "ping", "status", "uptime", "hostname",
    "disk", "memory", "echo", "logs", "internet", "public_ip"
}

def check_auth(value):
    expected = f"Bearer {TOKEN}"
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="unauthorized")

class Command(BaseModel):
    container: str
    bot: str
    command: str
    args: list = Field(default_factory=list)

class IPAuditRequest(BaseModel):
    container: str = "all"

def snapshot():
    result = []

    ip_counts = {}
    for (container_name, bot_name), item in bot_ip_cache.items():
        ip = item.get("public_ip")
        if ip:
            ip_counts[ip] = ip_counts.get(ip, 0) + 1
    for name in sorted(agents):
        item = agents[name]
        connected = item.get("ws") is not None
        result.append({
            "name": name,
            "online": connected,
            "last_seen": item.get("last_seen", 0),
            "bots": [
                {
                    "bot": bot,
                    "online": connected,
                    "public_ip": bot_ip_cache.get((name, bot), {}).get("public_ip"),
                    "public_ip_checked_at": bot_ip_cache.get((name, bot), {}).get("checked_at", 0),
                    "ip_duplicate": bool(
                        bot_ip_cache.get((name, bot), {}).get("public_ip")
                        and ip_counts.get(bot_ip_cache.get((name, bot), {}).get("public_ip"), 0) > 1
                    ),
                    "ip_shared_count": ip_counts.get(
                        bot_ip_cache.get((name, bot), {}).get("public_ip"), 0
                    ),
                    "tor_test": name == "container1" and bot == "bot-01",
                    "tor_test_state": tor_test_state if name == "container1" and bot == "bot-01" else None,
                }
                for bot in sorted(item.get("bots", set()))
            ],
        })
    return result

def dashboard_snapshot_payload():
    data = snapshot()
    return {
        "type": "dashboard_snapshot",
        "containers": data,
        "total_containers": len(data),
        "total_bots": sum(len(x["bots"]) for x in data),
        "online_containers": sum(1 for x in data if x["online"]),
        "online_bots": sum(
            1 for container in data for bot in container["bots"] if bot["online"]
        ),
        "updated_at": time.time(),
    }

async def broadcast_dashboard():
    if not dashboard_clients:
        return

    payload = json.dumps(dashboard_snapshot_payload())
    stale = []

    for ws in list(dashboard_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            stale.append(ws)

    for ws in stale:
        dashboard_clients.discard(ws)

async def dashboard_heartbeat():
    await asyncio.sleep(20)
    while True:
        try:
            await broadcast_dashboard()
        except Exception as exc:
            print(f"[dashboard-ws] heartbeat falhou: {exc}", flush=True)
        await asyncio.sleep(30)

@app.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket):
    await websocket.accept()

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        auth = json.loads(raw)

        if auth.get("type") != "auth":
            await websocket.close(code=4401)
            return

        received = str(auth.get("token", ""))
        if not secrets.compare_digest(received, TOKEN):
            await websocket.send_text(json.dumps({
                "type": "auth_error",
                "error": "unauthorized",
            }))
            await websocket.close(code=4401)
            return

        dashboard_clients.add(websocket)
        await websocket.send_text(json.dumps(dashboard_snapshot_payload()))
        print(f"[dashboard-ws] cliente conectado • {len(dashboard_clients)} ativo(s)", flush=True)

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({
                    "type": "pong",
                    "ts": time.time(),
                }))

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        print(f"[dashboard-ws] erro: {exc}", flush=True)
    finally:
        dashboard_clients.discard(websocket)
        print(f"[dashboard-ws] cliente desconectado • {len(dashboard_clients)} ativo(s)", flush=True)

@app.get("/health")
async def health():
    data = snapshot()
    return {
        "ok": True,
        "containers": len(data),
        "online_containers": sum(1 for x in data if x["online"]),
        "bots": sum(len(x["bots"]) for x in data),
    }

@app.get("/api/bots")
async def bots(authorization: str | None = Header(default=None)):
    check_auth(authorization)
    data = snapshot()
    return {
        "ok": True,
        "containers": data,
        "total_containers": len(data),
        "total_bots": sum(len(x["bots"]) for x in data),
    }

async def execute_one(container_name, bot, command_name, args):
    agent = agents.get(container_name)
    if not agent:
        return {"ok": False, "container": container_name, "bot": bot, "error": "container_not_registered"}

    ws = agent.get("ws")
    if ws is None:
        return {"ok": False, "container": container_name, "bot": bot, "error": "container_offline"}

    if bot not in agent.get("bots", set()):
        return {"ok": False, "container": container_name, "bot": bot, "error": "bot_not_registered"}

    request_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future

    payload = {
        "type": "command",
        "id": request_id,
        "bot": bot,
        "command": command_name,
        "args": args,
    }

    lock = agent_locks.setdefault(container_name, asyncio.Lock())

    try:
        async with lock:
            await ws.send_text(json.dumps(payload))
        result = await asyncio.wait_for(future, timeout=15)
        return {
            "ok": bool(result.get("ok")),
            "container": container_name,
            "bot": bot,
            "result": result,
        }
    except asyncio.TimeoutError:
        await request_agent_rotation(container_name, bot, "bot_timeout")
        return {"ok": False, "container": container_name, "bot": bot, "error": "bot_timeout"}
    except Exception as exc:
        return {"ok": False, "container": container_name, "bot": bot, "error": str(exc)}
    finally:
        pending.pop(request_id, None)

async def request_agent_rotation(container_name, bot, reason="bot_timeout"):
    """Pede ao agente que reconecte o bot por uma rota de IP diferente."""
    agent = agents.get(container_name)
    if not agent:
        return False
    ws = agent.get("ws")
    if ws is None:
        return False
    try:
        await ws.send_text(json.dumps({"type": "rotate", "bot": bot, "reason": reason}))
        print(f"[rotate] {container_name}/{bot} solicitado ({reason})", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[rotate] falha ao solicitar {container_name}/{bot}: {exc}", flush=True)
        return False

async def collect_ip_audit(container_filter="all"):
    targets = []

    if container_filter == "all":
        for container_name, agent in sorted(agents.items()):
            if agent.get("ws") is None:
                continue
            for bot in sorted(agent.get("bots", set())):
                targets.append((container_name, bot))
    else:
        agent = agents.get(container_filter)
        if not agent:
            raise HTTPException(status_code=404, detail="container_not_registered")
        if agent.get("ws") is None:
            raise HTTPException(status_code=409, detail="container_offline")
        for bot in sorted(agent.get("bots", set())):
            targets.append((container_filter, bot))

    if not targets:
        raise HTTPException(status_code=404, detail="no_targets")

    results = await asyncio.gather(*[
        execute_one(container_name, bot, "public_ip", [])
        for container_name, bot in targets
    ])

    remember_public_ip_results(results)

    ip_to_targets = {}
    failures = []
    for item in results:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        ip = result.get("public_ip")
        if item.get("ok") and ip:
            ip_to_targets.setdefault(ip, []).append({
                "container": item.get("container"),
                "bot": item.get("bot"),
            })
        else:
            failures.append({
                "container": item.get("container"),
                "bot": item.get("bot"),
                "error": item.get("error") or result.get("error") or "public_ip_failed",
            })

    duplicate_groups = [
        {"ip": ip, "count": len(owners), "bots": owners}
        for ip, owners in sorted(ip_to_targets.items())
        if len(owners) > 1
    ]
    unique_groups = [
        {"ip": ip, "count": 1, "bots": owners}
        for ip, owners in sorted(ip_to_targets.items())
        if len(owners) == 1
    ]

    duplicate_bot_count = sum(group["count"] for group in duplicate_groups)

    return {
        "ok": len(failures) == 0,
        "scope": container_filter,
        "checked": len(results),
        "successful": len(results) - len(failures),
        "failed": len(failures),
        "distinct_ips": len(ip_to_targets),
        "duplicate_ip_count": len(duplicate_groups),
        "duplicate_bot_count": duplicate_bot_count,
        "duplicate_groups": duplicate_groups,
        "unique_groups": unique_groups,
        "failures": failures,
        "checked_at": time.time(),
    }

@app.post("/api/ip-audit")
async def ip_audit(data: IPAuditRequest, authorization: str | None = Header(default=None)):
    check_auth(authorization)
    container_filter = data.container.strip() or "all"
    result = await collect_ip_audit(container_filter)

    print(
        f"[ip-audit] scope={container_filter} • "
        f"{result['successful']}/{result['checked']} OK • "
        f"{result['distinct_ips']} IP(s) • "
        f"{result['duplicate_ip_count']} IP(s) duplicado(s) • "
        f"{result['duplicate_bot_count']} bot(s) em grupos duplicados",
        flush=True,
    )
    return result

@app.post("/api/command")
async def command(data: Command, authorization: str | None = Header(default=None)):
    check_auth(authorization)

    command_name = data.command.strip().lower()
    if command_name not in ALLOWED:
        raise HTTPException(status_code=400, detail={
            "error": "command_not_allowed",
            "allowed": sorted(ALLOWED),
        })

    targets = []

    if data.container == "all":
        for container_name, agent in sorted(agents.items()):
            if agent.get("ws") is None:
                continue
            if data.bot == "all":
                for bot in sorted(agent.get("bots", set())):
                    targets.append((container_name, bot))
            elif data.bot in agent.get("bots", set()):
                targets.append((container_name, data.bot))
    else:
        agent = agents.get(data.container)
        if not agent:
            raise HTTPException(status_code=404, detail="container_not_registered")

        if data.bot == "all":
            for bot in sorted(agent.get("bots", set())):
                targets.append((data.container, bot))
        else:
            targets.append((data.container, data.bot))

    if not targets:
        raise HTTPException(status_code=404, detail="no_targets")

    # Limite defensivo para evitar lotes acidentais gigantes.
    if len(targets) > 500:
        raise HTTPException(status_code=400, detail="too_many_targets")

    results = await asyncio.gather(*[
        execute_one(container_name, bot, command_name, data.args)
        for container_name, bot in targets
    ])

    if command_name == "public_ip":
        remember_public_ip_results(results)
        await broadcast_dashboard()

    succeeded = sum(1 for item in results if item.get("ok"))
    failed = len(results) - succeeded

    return {
        "ok": failed == 0,
        "targets": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


@app.websocket("/ws/agent")
async def agent(websocket: WebSocket):
    await websocket.accept()
    name = None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        auth = json.loads(raw)

        if auth.get("type") != "auth":
            await websocket.close(code=4401)
            return

        received = str(auth.get("token", ""))
        if not secrets.compare_digest(received, TOKEN):
            await websocket.send_text(json.dumps({"ok": False, "error": "unauthorized"}))
            await websocket.close(code=4401)
            return

        name = str(auth.get("container", "")).strip()
        if not name or len(name) > 64:
            await websocket.send_text(json.dumps({"ok": False, "error": "invalid_container"}))
            await websocket.close(code=4400)
            return

        bots = {
            str(bot) for bot in auth.get("bots", [])
            if isinstance(bot, str) and bot.startswith("bot-")
        }

        old = agents.get(name, {}).get("ws")
        if old is not None and old is not websocket:
            try:
                await old.close(code=4000)
            except Exception:
                pass

        agents[name] = {
            "ws": websocket,
            "bots": bots,
            "last_seen": time.time(),
        }
        agent_locks.setdefault(name, asyncio.Lock())

        await websocket.send_text(json.dumps({"ok": True, "container": name}))
        print(f"[agent] {name} conectado com {len(bots)} bots", flush=True)
        await broadcast_dashboard()
        if name == "container1":
            asyncio.create_task(delayed_tor_reconnect_test())

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            current = agents.get(name)
            if current and current.get("ws") is websocket:
                current["last_seen"] = time.time()

            if msg.get("type") == "result":
                future = pending.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg.get("result", msg))

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        print(f"[agent] {name or 'desconhecido'} erro: {exc}", flush=True)
    finally:
        if name:
            current = agents.get(name)
            if current and current.get("ws") is websocket:
                current["ws"] = None
                current["last_seen"] = time.time()
                print(f"[agent] {name} desconectado", flush=True)
                await broadcast_dashboard()

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#070b12">
<title>BotVertra Control</title>
<style>
:root{
  --bg:#070b12;
  --bg2:#0b111c;
  --panel:#0e1623;
  --panel2:#111c2b;
  --border:#1d2a3d;
  --border2:#26374f;
  --text:#eef5ff;
  --muted:#8190a8;
  --muted2:#a6b4c8;
  --green:#56e6a5;
  --green-bg:rgba(86,230,165,.11);
  --red:#ff6f87;
  --red-bg:rgba(255,111,135,.11);
  --blue:#6aa9ff;
  --blue-bg:rgba(106,169,255,.10);
  --purple:#a98bff;
  --shadow:0 20px 50px rgba(0,0,0,.25);
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{
  margin:0;
  min-height:100%;
  background:
    radial-gradient(circle at 12% 0%,rgba(75,121,255,.10),transparent 30rem),
    radial-gradient(circle at 92% 4%,rgba(155,101,255,.08),transparent 28rem),
    var(--bg);
  color:var(--text);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
button,input,select{font:inherit}
button{cursor:pointer}
button:disabled{cursor:not-allowed;opacity:.55}
.shell{max-width:1320px;margin:0 auto;padding:22px}
.header{
  display:flex;justify-content:space-between;gap:18px;align-items:center;
  padding:4px 0 18px;border-bottom:1px solid rgba(255,255,255,.05)
}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{
  width:42px;height:42px;border-radius:13px;display:grid;place-items:center;
  background:linear-gradient(145deg,rgba(106,169,255,.22),rgba(169,139,255,.16));
  border:1px solid rgba(125,165,255,.22);box-shadow:inset 0 1px rgba(255,255,255,.05)
}
.logo svg{width:23px;height:23px}
.brand-copy{min-width:0}
.brand h1{font-size:18px;margin:0;font-weight:750;letter-spacing:-.02em}
.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}
.live-pill{
  display:flex;align-items:center;gap:8px;padding:8px 11px;border:1px solid var(--border);
  border-radius:999px;background:rgba(11,17,28,.75);color:var(--muted2);font-size:12px;white-space:nowrap
}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.online{background:var(--green);box-shadow:0 0 0 4px rgba(86,230,165,.09)}
.dot.offline{background:var(--red)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}
.stat{
  background:linear-gradient(180deg,rgba(17,28,43,.94),rgba(11,18,29,.94));
  border:1px solid var(--border);border-radius:16px;padding:15px 16px;box-shadow:var(--shadow)
}
.stat .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.stat .value{font-size:24px;font-weight:760;letter-spacing:-.04em;margin-top:5px}
.stat .sub{color:var(--muted);font-size:11px;margin-top:3px}
.panel{
  background:linear-gradient(180deg,rgba(14,22,35,.96),rgba(10,16,26,.96));
  border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow)
}
.auth{padding:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.auth .field-wrap{position:relative;flex:1;min-width:240px}
.auth input{
  width:100%;padding:11px 42px 11px 12px;background:#0a111c;border:1px solid var(--border2);
  color:var(--text);border-radius:11px;outline:none
}
.auth input:focus,.console-grid input:focus,.console-grid select:focus{
  border-color:#476fbe;box-shadow:0 0 0 3px rgba(71,111,190,.12)
}
.eye{
  position:absolute;right:6px;top:50%;transform:translateY(-50%);border:0;background:transparent;color:var(--muted);
  width:34px;height:34px;border-radius:8px;padding:0
}
.btn{
  border:1px solid var(--border2);background:#121c2b;color:var(--text);padding:10px 13px;border-radius:10px;
  transition:.15s ease
}
.btn:hover{background:#172439;border-color:#355078}
.btn.primary{
  background:linear-gradient(135deg,#2c62c8,#6649b9);border-color:#557ad1;color:#fff;font-weight:650
}
.btn.primary:hover{filter:brightness(1.08)}
.btn.ghost{background:transparent}
.auth-state{color:var(--muted);font-size:12px;margin-left:auto}
.console{padding:17px;margin-bottom:18px}
.section-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:14px}
.section-head h2{font-size:14px;margin:0;font-weight:700}
.section-head p{font-size:12px;color:var(--muted);margin:4px 0 0}
.console-grid{
  display:grid;grid-template-columns:minmax(170px,.9fr) minmax(170px,.9fr) minmax(260px,2fr) auto;
  gap:9px
}
.console-grid select,.console-grid input{
  min-width:0;width:100%;background:#09101a;border:1px solid var(--border2);color:var(--text);
  padding:11px 12px;border-radius:11px;outline:none
}
.quick{display:flex;flex-wrap:wrap;gap:7px;margin-top:11px}
.chip{
  border:1px solid var(--border);background:#0b1420;color:var(--muted2);padding:7px 10px;border-radius:999px;font-size:11px
}
.chip:hover{color:var(--text);border-color:#385172;background:#101b2a}
.content-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(330px,.85fr);gap:18px;align-items:start}
.containers{display:flex;flex-direction:column;gap:13px}
.container-card{overflow:hidden}
.container-top{
  display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 16px;border-bottom:1px solid rgba(255,255,255,.045)
}
.container-title{display:flex;align-items:center;gap:10px;min-width:0}
.container-icon{
  width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:var(--blue-bg);color:var(--blue);
  border:1px solid rgba(106,169,255,.15)
}
.container-name{font-weight:720;font-size:14px}
.container-meta{font-size:11px;color:var(--muted);margin-top:2px}
.badge{
  display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:6px 9px;font-size:10px;font-weight:700;
  letter-spacing:.04em
}
.badge.online{background:var(--green-bg);color:var(--green);border:1px solid rgba(86,230,165,.17)}
.badge.offline{background:var(--red-bg);color:var(--red);border:1px solid rgba(255,111,135,.17)}
.bulk-actions{padding:10px 14px;display:flex;gap:7px;flex-wrap:wrap;background:rgba(255,255,255,.012)}
.bulk-actions .btn{font-size:11px;padding:7px 9px}
.bots-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:9px;padding:13px}
.bot{
  border:1px solid var(--border);border-radius:13px;padding:11px;background:linear-gradient(180deg,#0c1521,#0a111b)
}
.bot-head{display:flex;justify-content:space-between;gap:8px;align-items:center}
.bot-name{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;font-weight:700}
.bot-state-wrap{display:flex;align-items:center;gap:6px}
.bot-status{font-size:10px;color:var(--green)}
.ip-wifi-alert{
  display:inline-flex;align-items:center;justify-content:center;
  width:16px;height:16px;color:var(--red);font-size:14px;line-height:1;
  filter:drop-shadow(0 0 5px rgba(255,111,135,.25));
}
.ip-wifi-alert[hidden]{display:none}
.ip-wifi-alert::before{content:"⌁";font-weight:800}
.bot.ip-duplicate{
  border-color:rgba(255,111,135,.42)!important;
  background:linear-gradient(180deg,rgba(255,111,135,.10),rgba(255,111,135,.045))!important;
  box-shadow:inset 0 0 0 1px rgba(255,111,135,.05),0 8px 24px rgba(255,45,85,.05);
}
.bot.ip-duplicate:hover{
  border-color:rgba(255,111,135,.58)!important;
}
.bot-ip{margin-top:7px;padding:6px 8px;border-radius:8px;border:1px solid rgba(106,169,255,.18);background:rgba(106,169,255,.07);color:#9cc4ff;font-size:10px;line-height:1.35;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.bot-ip.pending{color:var(--muted);border-color:var(--border);background:rgba(255,255,255,.02)}
.tor-note{margin-top:7px;padding:6px 8px;border-radius:8px;border:1px solid rgba(169,139,255,.22);background:rgba(169,139,255,.08);color:#c8b8ff;font-size:10px;line-height:1.35}
.bot-actions{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:10px}
.bot-actions button{
  border:1px solid var(--border);background:#0f1927;color:var(--muted2);border-radius:8px;padding:7px 5px;font-size:10px
}
.bot-actions button:hover{color:#fff;border-color:#385172;background:#142236}
.output{position:sticky;top:14px;overflow:hidden}
.output-head{padding:14px 15px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:8px}
.output-head h2{font-size:13px;margin:0}
.output-status{font-size:11px;color:var(--muted)}
.terminal{
  margin:0;min-height:340px;max-height:640px;overflow:auto;padding:15px;background:#050910;color:#b9c8dc;
  font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word
}
.empty{
  padding:32px 18px;text-align:center;color:var(--muted);border:1px dashed var(--border2);border-radius:16px
}
.empty strong{display:block;color:var(--muted2);margin-bottom:5px}
.footer{color:#66748a;text-align:center;font-size:10px;padding:22px 0 8px}
.skeleton{animation:pulse 1.25s ease-in-out infinite alternate}
@keyframes pulse{to{opacity:.48}}
@media (max-width:980px){
  .content-grid{grid-template-columns:1fr}
  .output{position:static}
  .console-grid{grid-template-columns:1fr 1fr}
  .console-grid input{grid-column:1/-1}
  .console-grid .run-btn{grid-column:1/-1}
}
@media (max-width:680px){
  .shell{padding:14px}
  .header{align-items:flex-start}
  .brand p{display:none}
  .stats{grid-template-columns:1fr 1fr}
  .stat{padding:13px}
  .stat .value{font-size:21px}
  .auth{align-items:stretch}
  .auth .field-wrap{flex-basis:100%}
  .auth .btn{flex:1}
  .auth-state{width:100%;margin-left:0}
  .console-grid{grid-template-columns:1fr}
  .console-grid input,.console-grid .run-btn{grid-column:auto}
  .bots-grid{grid-template-columns:1fr 1fr}
  .content-grid{gap:14px}
}
@media (max-width:430px){
  .live-pill{padding:7px 9px}
  .stats{gap:8px}
  .bots-grid{grid-template-columns:1fr}
  .container-top{align-items:flex-start}
}
/* dashboard-clean-v2 */
.output-tools{display:flex;align-items:center;gap:8px}
.terminal-copy{padding:6px 9px;font-size:10px;white-space:nowrap}

.shell{max-width:1500px;padding:18px}
.header{position:sticky;top:0;z-index:20;padding:12px 0 14px;background:linear-gradient(180deg,rgba(7,11,18,.96),rgba(7,11,18,.82),transparent);backdrop-filter:blur(14px)}
.stats{gap:10px;margin:12px 0}
.stat{padding:12px 14px;border-radius:14px}
.stat .value{font-size:22px}
.panel{border-radius:16px}
.auth{padding:10px 12px;gap:8px;margin-bottom:12px}
.commandbar{padding:12px;margin-bottom:14px}
.commandbar .section-head{margin-bottom:10px}
.commandbar .section-head p{display:none}
.commandbar .quick{margin-top:9px}
.commandbar .quick .chip:nth-child(n+6):not(#auditAllIps){display:none}
.content-grid{
  display:grid;
  grid-template-columns:minmax(0,1fr) 390px;
  gap:16px;
  align-items:start;
}
.content-grid>section{min-width:0}
.output{
  position:sticky;
  top:82px;
  max-height:calc(100vh - 100px);
  overflow:hidden;
  display:flex;
  flex-direction:column;
  min-height:520px;
}
.output-head{flex:0 0 auto}
.terminal{
  flex:1 1 auto;
  min-height:420px;
  max-height:none;
  overflow:auto;
  border-radius:12px;
  padding:14px;
  font-size:12px;
  line-height:1.55;
}
.containers{display:grid;gap:12px}
.container-card{padding:14px}
.container-top{margin-bottom:10px}
.container-icon{width:36px;height:36px;border-radius:11px}
.container-meta{font-size:11px}
.bulk-actions{gap:6px;margin:9px 0 12px}
.bulk-actions .btn{padding:7px 10px;font-size:11px}
.bulk-more{position:relative}
.bulk-more>summary,.bot-more>summary{
  list-style:none;cursor:pointer;user-select:none;
}
.bulk-more>summary::-webkit-details-marker,.bot-more>summary::-webkit-details-marker{display:none}
.more-menu{
  display:flex;flex-wrap:wrap;gap:6px;margin-top:7px;
  padding:8px;border:1px solid var(--border);border-radius:11px;
  background:rgba(5,9,15,.72)
}
.bots-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
  gap:8px
}
.bot{
  padding:10px;
  border-radius:12px;
  min-height:0;
  transition:border-color .16s ease,background .16s ease,transform .16s ease;
}
.bot:hover{transform:translateY(-1px)}
.bot-head{margin-bottom:6px}
.bot-name{font-size:12px}
.bot-ip{margin-top:5px;padding:5px 7px;font-size:10px}
.bot-actions{display:flex;gap:5px;margin-top:8px;align-items:center}
.bot-actions button{padding:5px 8px;font-size:10px}
.bot-more{margin-left:auto;position:relative}
.bot-more .more-menu{
  position:absolute;right:0;top:100%;z-index:12;
  width:210px;box-shadow:0 16px 38px rgba(0,0,0,.34)
}
.bot-more:not([open]) .more-menu{display:none}
.tor-note{font-size:9px;padding:5px 7px;margin-top:6px}
.section-head{margin-bottom:9px}
.section-head h2{font-size:15px}
.section-head p{font-size:11px}
.terminal-fab{display:none}

@media (max-width:1050px){
  .content-grid{grid-template-columns:minmax(0,1fr) 340px}
  .bots-grid{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
}
@media (max-width:820px){
  .shell{padding:12px}
  .header{top:0;padding-top:8px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .auth{flex-wrap:wrap}
  .commandbar .console-grid{grid-template-columns:1fr 1fr}
  .commandbar .console-grid input{grid-column:1/-1}
  .commandbar .console-grid .run-btn{grid-column:1/-1}
  .content-grid{display:block}
  .output{
    position:fixed;
    z-index:50;
    left:10px;right:10px;bottom:10px;top:auto;
    max-height:72vh;min-height:0;
    transform:translateY(calc(100% + 24px));
    transition:transform .2s ease;
    box-shadow:0 24px 70px rgba(0,0,0,.55);
  }
  .output.mobile-open{transform:translateY(0)}
  .terminal{min-height:280px;max-height:52vh}
  .terminal-fab{
    display:inline-flex;align-items:center;justify-content:center;
    position:fixed;right:16px;bottom:16px;z-index:55;
    border:1px solid var(--border2);background:var(--panel2);color:var(--text);
    border-radius:999px;padding:10px 14px;box-shadow:0 12px 30px rgba(0,0,0,.38);
    font-weight:700;font-size:12px
  }
  .containers{padding-bottom:74px}
}
@media (max-width:520px){
  .bots-grid{grid-template-columns:1fr}
  .brand p{display:none}
  .live-pill{font-size:10px}
  .commandbar .quick{display:grid;grid-template-columns:1fr 1fr}
  .commandbar .quick .chip{width:100%}
}
/* dashboard-intuitive-v3 */
.panel-soft{
  border:1px solid var(--border);
  background:rgba(255,255,255,.025);
  border-radius:14px;
}
.infra-head{align-items:center}
.compact-btn{padding:7px 10px;font-size:11px}
.infra-toolbar{
  display:grid;
  grid-template-columns:minmax(240px,1fr) auto auto;
  align-items:center;
  gap:10px;
  padding:10px;
  margin-bottom:12px;
  position:sticky;
  top:74px;
  z-index:14;
  backdrop-filter:blur(12px);
}
.search-wrap{position:relative;display:flex;align-items:center}
.search-wrap input{
  width:100%;padding:10px 34px 10px 34px;border-radius:11px;
  background:rgba(4,8,14,.72);border:1px solid var(--border);
  color:var(--text);outline:none
}
.search-wrap input:focus{border-color:rgba(106,169,255,.52);box-shadow:0 0 0 3px rgba(106,169,255,.08)}
.search-icon{position:absolute;left:12px;color:var(--muted);pointer-events:none}
.search-clear{
  position:absolute;right:8px;width:25px;height:25px;border:0;border-radius:8px;
  background:transparent;color:var(--muted);font-size:18px;cursor:pointer
}
.search-clear:hover{background:rgba(255,255,255,.05);color:var(--text)}
.filter-group{display:flex;gap:6px;flex-wrap:wrap}
.filter-chip{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:999px;padding:7px 10px;font-size:10px;font-weight:700;cursor:pointer
}
.filter-chip:hover{color:var(--text);border-color:var(--border2)}
.filter-chip.active{
  color:#dceaff;background:rgba(106,169,255,.12);border-color:rgba(106,169,255,.35)
}
.filter-count{font-size:11px;color:var(--muted);white-space:nowrap}
.container-card.is-collapsed .bulk-actions,
.container-card.is-collapsed .bots-grid{display:none}
.container-toggle{
  border:0;background:transparent;color:var(--muted);cursor:pointer;
  font-size:14px;padding:6px 8px;border-radius:8px
}
.container-toggle:hover{background:rgba(255,255,255,.05);color:var(--text)}
.container-actions-top{display:flex;align-items:center;gap:7px}
.bot{cursor:pointer}
.bot.selected{
  outline:2px solid rgba(106,169,255,.42);
  outline-offset:1px;
  border-color:rgba(106,169,255,.42)
}
.bot-no-results{
  padding:22px;text-align:center;color:var(--muted);
  border:1px dashed var(--border);border-radius:14px
}
.legend{
  display:flex;gap:12px;flex-wrap:wrap;margin:0 0 10px 2px;
  color:var(--muted);font-size:10px
}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend-dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.legend-alert{width:8px;height:8px;border-radius:3px;background:rgba(255,111,135,.7)}
@media (max-width:980px){
  .infra-toolbar{grid-template-columns:1fr;position:relative;top:auto}
  .filter-count{justify-self:start}
}
@media (max-width:520px){
  .filter-group{display:grid;grid-template-columns:1fr 1fr}
  .filter-chip{width:100%}
  .infra-head{align-items:flex-start}
}
</style>
</head>
<body>
<div class="shell">
  <header class="header">
    <div class="brand">
      <div class="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M5 7.5 12 3l7 4.5v9L12 21l-7-4.5v-9Z" stroke="currentColor" stroke-width="1.5"/>
          <path d="M8.5 9.5h7M8.5 12h7M8.5 14.5h4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
      </div>
      <div class="brand-copy">
        <h1>BotVertra Control</h1>
        <p>Gerenciamento centralizado dos workers VertraCloud</p>
      </div>
    </div>
    <div class="live-pill"><span id="globalDot" class="dot"></span><span id="globalText">Aguardando autenticação</span></div>
  </header>

  <section class="stats" aria-label="Resumo">
    <div class="stat"><div class="label">Containers</div><div id="statContainers" class="value">—</div><div id="statContainersSub" class="sub">sem dados</div></div>
    <div class="stat"><div class="label">Bots</div><div id="statBots" class="value">—</div><div id="statBotsSub" class="sub">sem dados</div></div>
    <div class="stat"><div class="label">Online</div><div id="statOnline" class="value">—</div><div class="sub">workers disponíveis</div></div>
    <div class="stat"><div class="label">IPs exclusivos</div><div id="statRefresh" class="value">—</div><div id="statIpSub" class="sub">sem dados</div></div>
  </section>

  <section class="panel auth">
    <div class="field-wrap">
      <input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="CONTROLLER_TOKEN">
      <button id="toggleToken" class="eye" type="button" aria-label="Mostrar ou ocultar token">◉</button>
    </div>
    <button id="saveBtn" class="btn primary" type="button">Salvar token</button>
    <button id="refreshBtn" class="btn" type="button">Atualizar</button>
    <button id="clearBtn" class="btn ghost" type="button">Limpar</button>
    <span id="authState" class="auth-state">Insira o token para conectar</span>
  </section>

  <section class="panel console commandbar">
    <div class="section-head">
      <div>
        <h2>Console remoto</h2>
        <p>Execute comandos permitidos em um bot, um container inteiro ou em todos.</p>
      </div>
    </div>
    <div class="console-grid">
      <select id="containerSel" aria-label="Container"></select>
      <select id="botSel" aria-label="Bot"></select>
      <input id="command" autocomplete="off" spellcheck="false" placeholder="Ex.: status | logs 100 | echo oi">
      <button id="runBtn" class="btn primary run-btn" type="button">Executar</button>
    </div>
    <div class="quick" id="quickCommands">
      <button class="chip" data-cmd="ping" type="button">Ping</button>
      <button class="chip" data-cmd="status" type="button">Status</button>
      <button class="chip" data-cmd="public_ip" type="button">IP público</button>
      <button class="chip" data-cmd="logs 40" type="button">Logs</button>
      <button class="chip" id="auditAllIps" type="button">Auditar IPs</button>
      <button class="chip" data-cmd="internet" type="button">Internet</button>
    </div>
  </section>

  <main class="content-grid">
    <section>
      <div class="section-head infra-head">
        <div>
          <h2>Infraestrutura</h2>
          <p id="infraSubtitle">Aguardando dados dos containers.</p>
        </div>
        <button id="collapseAllBtn" class="btn ghost compact-btn" type="button">Recolher todos</button>
      </div>

      <div class="infra-toolbar panel-soft">
        <div class="search-wrap">
          <span class="search-icon">⌕</span>
          <input id="botSearch" autocomplete="off" spellcheck="false" placeholder="Buscar bot, container ou IP…">
          <button id="clearSearchBtn" class="search-clear" type="button" title="Limpar busca">×</button>
        </div>
        <div class="filter-group" id="botFilters" aria-label="Filtros">
          <button class="filter-chip active" data-filter="all" type="button">Todos</button>
          <button class="filter-chip" data-filter="alert" type="button">⚠ IP repetido</button>
          <button class="filter-chip" data-filter="online" type="button">● Online</button>
          <button class="filter-chip" data-filter="offline" type="button">● Offline</button>
        </div>
        <div id="filterCount" class="filter-count">0 bots</div>
      </div>

      <div class="legend">
        <span><i class="legend-dot"></i> bot online</span>
        <span><i class="legend-alert"></i> IP compartilhado</span>
        <span>Toque no card para selecionar no console</span>
      </div>
      <div id="containers" class="containers">
        <div class="empty"><strong>Nenhum dado carregado</strong>Salve o token para consultar os containers.</div>
      </div>
    </section>

    <aside class="panel output">
      <div class="output-head">
        <h2>Terminal</h2>
        <div class="output-tools">
          <span id="outputStatus" class="output-status">pronto</span>
          <button id="copyTerminalBtn" class="btn ghost terminal-copy" type="button">Copiar tudo</button>
          <button id="clearTerminalBtn" class="btn ghost terminal-copy" type="button">Limpar</button>
        </div>
      </div>
      <pre id="out" class="terminal">BotVertra pronto.</pre>
    </aside>
  </main>
  <button id="terminalFab" class="terminal-fab" type="button">⌘ Terminal</button>

  <div class="footer">BotVertra Controller • acesso autenticado • comandos allowlist</div>
</div>

<script>
const token = document.getElementById('token');
const saveBtn = document.getElementById('saveBtn');
const refreshBtn = document.getElementById('refreshBtn');
const clearBtn = document.getElementById('clearBtn');
const toggleToken = document.getElementById('toggleToken');
const authState = document.getElementById('authState');
const containersEl = document.getElementById('containers');
const containerSel = document.getElementById('containerSel');
const botSel = document.getElementById('botSel');
const commandInput = document.getElementById('command');
const runBtn = document.getElementById('runBtn');
const out = document.getElementById('out');
const outputStatus = document.getElementById('outputStatus');
const copyTerminalBtn=document.getElementById('copyTerminalBtn');
const globalDot = document.getElementById('globalDot');
const globalText = document.getElementById('globalText');
const infraSubtitle = document.getElementById('infraSubtitle');
const statContainers = document.getElementById('statContainers');
const statContainersSub = document.getElementById('statContainersSub');
const statBots = document.getElementById('statBots');
const statBotsSub = document.getElementById('statBotsSub');
const statOnline = document.getElementById('statOnline');
const statRefresh = document.getElementById('statRefresh');
const terminalFab=document.getElementById('terminalFab');
const terminalPanel=document.querySelector('.output');
const botSearch=document.getElementById('botSearch');
const clearSearchBtn=document.getElementById('clearSearchBtn');
const botFilters=document.getElementById('botFilters');
const filterCount=document.getElementById('filterCount');
const collapseAllBtn=document.getElementById('collapseAllBtn');
const clearTerminalBtn=document.getElementById('clearTerminalBtn');
const statIpSub=document.getElementById('statIpSub');

let data = [];
let loading = false;
let liveSocket = null;
let liveRetry = null;
let botFilter = 'all';
let collapsedContainers = new Set();
let selectedTarget = {container:null, bot:null};

if(terminalFab && terminalPanel){
  terminalFab.addEventListener('click',()=>{
    terminalPanel.classList.toggle('mobile-open');
    terminalFab.textContent=terminalPanel.classList.contains('mobile-open')?'✕ Fechar':'⌘ Terminal';
  });
}

function readSavedToken(){
  try{return localStorage.getItem('botvertra_token') || ''}catch(e){return ''}
}
function writeSavedToken(value){
  try{localStorage.setItem('botvertra_token', value);return true}catch(e){return false}
}
function removeSavedToken(){
  try{localStorage.removeItem('botvertra_token')}catch(e){}
}
function headers(){
  return {'Authorization':'Bearer '+token.value.trim(),'Content-Type':'application/json'}
}
function setGlobal(ok,text){
  globalDot.className='dot '+(ok?'online':'offline');
  globalText.textContent=text;
}
async function copyTerminalAll(){
  const text=out.textContent || '';
  if(!text) return;

  let copied=false;
  try{
    if(navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(text);
      copied=true;
    }
  }catch(e){}

  if(!copied){
    try{
      const ta=document.createElement('textarea');
      ta.value=text;
      ta.setAttribute('readonly','');
      ta.style.position='fixed';
      ta.style.opacity='0';
      document.body.appendChild(ta);
      ta.select();
      copied=document.execCommand('copy');
      ta.remove();
    }catch(e){}
  }

  const old=copyTerminalBtn.textContent;
  copyTerminalBtn.textContent=copied?'Copiado ✓':'Falhou';
  setTimeout(()=>{copyTerminalBtn.textContent=old},1400);
}

if(copyTerminalBtn){
  copyTerminalBtn.addEventListener('click',copyTerminalAll);
}
if(clearTerminalBtn){
  clearTerminalBtn.addEventListener('click',()=>{
    setOutput('BotVertra pronto.','pronto');
  });
}

function revealTerminal(){
  if(window.matchMedia('(max-width:820px)').matches && terminalPanel){
    terminalPanel.classList.add('mobile-open');
    if(terminalFab) terminalFab.textContent='✕ Fechar';
  }
}
function setOutput(text,status='pronto'){

  out.textContent=text;
  outputStatus.textContent=status;
  if(status!=='pronto') revealTerminal();
  out.scrollTop=0;
}
function fmtTime(ts){
  if(!ts) return 'sem atividade';
  try{return new Date(ts*1000).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return '—'}
}
function nowLabel(){
  return new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
}

function applyLiveSnapshot(j){
  data=j.containers||[];
  renderStats();
  rebuildSelectors();
  renderContainers();
  authState.textContent='Tempo real ativo • '+(j.total_bots ?? data.reduce((n,c)=>n+c.bots.length,0))+' bots';
}

function disconnectLive(){
  if(liveRetry){
    clearTimeout(liveRetry);
    liveRetry=null;
  }
  if(liveSocket){
    try{liveSocket.onclose=null;liveSocket.close()}catch(e){}
    liveSocket=null;
  }
}

function scheduleLiveReconnect(){
  if(liveRetry || !token.value.trim()) return;
  liveRetry=setTimeout(()=>{
    liveRetry=null;
    connectLive();
  },2000);
}

function connectLive(){
  const value=token.value.trim();
  if(!value) return;

  disconnectLive();

  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(proto+'://'+location.host+'/ws/dashboard');
  liveSocket=ws;

  ws.addEventListener('open',()=>{
    ws.send(JSON.stringify({type:'auth',token:value}));
  });

  ws.addEventListener('message',(event)=>{
    try{
      const j=JSON.parse(event.data);
      if(j.type==='dashboard_snapshot'){
        applyLiveSnapshot(j);
      }else if(j.type==='auth_error'){
        authState.textContent='Token inválido no tempo real';
        setGlobal(false,'Falha na autenticação');
      }
    }catch(e){}
  });

  ws.addEventListener('close',()=>{
    if(liveSocket===ws) liveSocket=null;
    if(token.value.trim()){
      authState.textContent='Tempo real reconectando...';
      scheduleLiveReconnect();
    }
  });

  ws.addEventListener('error',()=>{
    try{ws.close()}catch(e){}
  });
}

token.value = readSavedToken();

toggleToken.addEventListener('click',()=>{
  token.type = token.type === 'password' ? 'text' : 'password';
  toggleToken.textContent = token.type === 'password' ? '◉' : '◎';
});

saveBtn.addEventListener('click',()=>{
  const value=token.value.trim();
  if(!value){
    authState.textContent='Digite o token.';
    setGlobal(false,'Sem autenticação');
    return;
  }
  const saved=writeSavedToken(value);
  authState.textContent=saved?'Token salvo neste navegador ✓':'Token em uso; navegador bloqueou o armazenamento';
  loadBots();
  connectLive();
});

refreshBtn.addEventListener('click',()=>loadBots(true));

clearBtn.addEventListener('click',()=>{
  disconnectLive();
  token.value='';
  removeSavedToken();
  data=[];
  renderStats();
  rebuildSelectors();
  containersEl.innerHTML='<div class="empty"><strong>Token removido</strong>Insira novamente para carregar a infraestrutura.</div>';
  authState.textContent='Token removido';
  setGlobal(false,'Sem autenticação');
});

function rebuildSelectors(){
  const prevContainer=containerSel.value;
  const prevBot=botSel.value;
  containerSel.innerHTML='';

  const all=document.createElement('option');
  all.value='all';
  all.textContent='Todos os containers';
  containerSel.appendChild(all);

  for(const c of data){
    const opt=document.createElement('option');
    opt.value=c.name;
    opt.textContent=c.name+(c.online?'':' • offline');
    containerSel.appendChild(opt);
  }

  if(prevContainer && [...containerSel.options].some(o=>o.value===prevContainer)){
    containerSel.value=prevContainer;
  }

  botSel.innerHTML='';
  const allBots=document.createElement('option');
  allBots.value='all';
  allBots.textContent=containerSel.value==='all'?'Todos os bots':'Todos deste container';
  botSel.appendChild(allBots);

  const selected=data.find(c=>c.name===containerSel.value);
  if(selected){
    for(const b of selected.bots){
      const opt=document.createElement('option');
      opt.value=b.bot;
      opt.textContent=b.bot;
      botSel.appendChild(opt);
    }
  }

  if(prevBot && [...botSel.options].some(o=>o.value===prevBot)){
    botSel.value=prevBot;
  }
}
containerSel.addEventListener('change',()=>{
  rebuildSelectors();
  selectedTarget={container:containerSel.value==='all'?null:containerSel.value,bot:null};
  renderContainers();
});
botSel.addEventListener('change',()=>{
  selectedTarget={
    container:containerSel.value==='all'?null:containerSel.value,
    bot:botSel.value==='all'?null:botSel.value
  };
  renderContainers();
});

if(botSearch){
  botSearch.addEventListener('input',renderContainers);
}
if(clearSearchBtn){
  clearSearchBtn.addEventListener('click',()=>{
    botSearch.value='';
    renderContainers();
    botSearch.focus();
  });
}
if(botFilters){
  botFilters.addEventListener('click',(e)=>{
    const bt=e.target.closest('[data-filter]');
    if(!bt) return;
    botFilter=bt.dataset.filter;
    for(const chip of botFilters.querySelectorAll('[data-filter]')){
      chip.classList.toggle('active',chip===bt);
    }
    renderContainers();
  });
}
if(collapseAllBtn){
  collapseAllBtn.addEventListener('click',()=>{
    if(data.length && collapsedContainers.size>=data.length){
      collapsedContainers.clear();
    }else{
      collapsedContainers=new Set(data.map(c=>c.name));
    }
    renderContainers();
    updateCollapseButton();
  });
}

document.addEventListener('click',(e)=>{
  for(const el of document.querySelectorAll('.bot-more[open],.bulk-more[open]')){
    if(!el.contains(e.target)) el.open=false;
  }
});

function normalizedSearch(){
  return (botSearch?.value || '').trim().toLowerCase();
}

function botMatchesFilters(container,b){
  const q=normalizedSearch();
  const searchable=(container.name+' '+b.bot+' '+(b.public_ip||'')).toLowerCase();
  if(q && !searchable.includes(q)) return false;

  if(botFilter==='alert' && !b.ip_duplicate) return false;
  if(botFilter==='online' && !b.online) return false;
  if(botFilter==='offline' && b.online) return false;
  return true;
}

function selectBotTarget(containerName,botName){
  selectedTarget={container:containerName,bot:botName};
  containerSel.value=containerName;
  rebuildSelectors();
  botSel.value=botName;
  commandInput.focus();
  renderContainers();
}

function updateCollapseButton(){
  const total=data.length;
  collapseAllBtn.textContent=total>0 && collapsedContainers.size>=total
    ? 'Expandir todos'
    : 'Recolher todos';
}

function renderStats(){
  const totalContainers=data.length;
  const onlineContainers=data.filter(c=>c.online).length;
  const totalBots=data.reduce((n,c)=>n+c.bots.length,0);
  const onlineBots=data.reduce((n,c)=>n+c.bots.filter(b=>b.online).length,0);

  statContainers.textContent=totalContainers || '0';
  statContainersSub.textContent=onlineContainers+' online';
  statBots.textContent=totalBots || '0';
  statBotsSub.textContent='registrados';
  statOnline.textContent=onlineBots || '0';

  const allBots=data.flatMap(c=>c.bots);
  const exclusiveIps=new Set(
    allBots.filter(b=>b.public_ip && !b.ip_duplicate).map(b=>b.public_ip)
  );
  const duplicateBots=allBots.filter(b=>b.ip_duplicate).length;
  statRefresh.textContent=exclusiveIps.size;
  if(statIpSub){
    statIpSub.textContent=duplicateBots
      ? duplicateBots+' bot'+(duplicateBots===1?'':'s')+' com IP repetido'
      : 'nenhum IP repetido';
  }

  if(totalContainers){
    infraSubtitle.textContent=onlineContainers+'/'+totalContainers+' containers online • '+onlineBots+'/'+totalBots+' bots disponíveis';
    setGlobal(onlineContainers>0,onlineContainers+' container'+(onlineContainers===1?'':'s')+' online');
  }else{
    infraSubtitle.textContent='Nenhum container registrado.';
    setGlobal(false,'Sem containers');
  }
}

function actionButton(label,handler){
  const bt=document.createElement('button');
  bt.type='button';
  bt.className='btn';
  bt.textContent=label;
  bt.addEventListener('click',handler);
  return bt;
}

function renderContainers(){
  containersEl.innerHTML='';

  if(!data.length){
    containersEl.innerHTML='<div class="empty"><strong>Nenhum container encontrado</strong>Verifique se os bridges estão conectados ao controller.</div>';
    if(filterCount) filterCount.textContent='0 bots';
    return;
  }

  let visibleTotal=0;

  for(const c of data){
    const visibleBots=c.bots.filter(b=>botMatchesFilters(c,b));
    const query=normalizedSearch();
    const containerMatches=query && c.name.toLowerCase().includes(query);

    if(!visibleBots.length && !containerMatches) continue;

    const botsToShow=containerMatches && !visibleBots.length ? c.bots : visibleBots;
    visibleTotal+=botsToShow.length;

    const card=document.createElement('article');
    card.className='panel container-card'+(collapsedContainers.has(c.name)?' is-collapsed':'');

    const top=document.createElement('div');
    top.className='container-top';

    const titleWrap=document.createElement('div');
    titleWrap.className='container-title';

    const icon=document.createElement('div');
    icon.className='container-icon';
    icon.textContent='▣';

    const texts=document.createElement('div');
    const name=document.createElement('div');
    name.className='container-name';
    name.textContent=c.name;

    const alerts=c.bots.filter(b=>b.ip_duplicate).length;
    const meta=document.createElement('div');
    meta.className='container-meta';
    meta.textContent=c.bots.length+' bots • '+alerts+' alerta'+(alerts===1?'':'s')+' • atividade '+fmtTime(c.last_seen);

    texts.append(name,meta);
    titleWrap.append(icon,texts);

    const topActions=document.createElement('div');
    topActions.className='container-actions-top';

    const badge=document.createElement('span');
    badge.className='badge '+(c.online?'online':'offline');
    badge.textContent=(c.online?'● ONLINE':'● OFFLINE');

    const toggle=document.createElement('button');
    toggle.type='button';
    toggle.className='container-toggle';
    toggle.title=collapsedContainers.has(c.name)?'Expandir container':'Recolher container';
    toggle.textContent=collapsedContainers.has(c.name)?'▾':'▴';
    toggle.addEventListener('click',(e)=>{
      e.stopPropagation();
      if(collapsedContainers.has(c.name)) collapsedContainers.delete(c.name);
      else collapsedContainers.add(c.name);
      renderContainers();
      updateCollapseButton();
    });

    topActions.append(badge,toggle);
    top.append(titleWrap,topActions);

    const bulk=document.createElement('div');
    bulk.className='bulk-actions';
    bulk.appendChild(actionButton('Ping',()=>run(c.name,'all','ping')));
    bulk.appendChild(actionButton('Status',()=>run(c.name,'all','status')));
    bulk.appendChild(actionButton('Auditar IPs',()=>auditIPs(c.name)));

    const bulkMore=document.createElement('details');
    bulkMore.className='bulk-more';
    const bulkSummary=document.createElement('summary');
    bulkSummary.className='btn ghost';
    bulkSummary.textContent='Mais ▾';
    const bulkMenu=document.createElement('div');
    bulkMenu.className='more-menu';

    for(const cmd of ['uptime','memory','disk','internet','public_ip']){
      bulkMenu.appendChild(actionButton(cmd,()=>run(c.name,'all',cmd)));
    }

    bulkMore.append(bulkSummary,bulkMenu);
    bulk.appendChild(bulkMore);

    const grid=document.createElement('div');
    grid.className='bots-grid';

    for(const b of botsToShow){
      const bot=document.createElement('div');
      const selected=selectedTarget.container===c.name && selectedTarget.bot===b.bot;
      bot.className='bot'+(b.ip_duplicate?' ip-duplicate':'')+(selected?' selected':'');
      bot.tabIndex=0;
      bot.title='Selecionar '+c.name+'/'+b.bot+' no console';

      bot.addEventListener('click',(e)=>{
        if(e.target.closest('button,details,summary')) return;
        selectBotTarget(c.name,b.bot);
      });
      bot.addEventListener('keydown',(e)=>{
        if(e.key==='Enter' || e.key===' '){
          e.preventDefault();
          selectBotTarget(c.name,b.bot);
        }
      });

      const head=document.createElement('div');
      head.className='bot-head';

      const botName=document.createElement('span');
      botName.className='bot-name';
      botName.textContent=b.bot;

      const stateWrap=document.createElement('span');
      stateWrap.className='bot-state-wrap';

      const botState=document.createElement('span');
      botState.className='bot-status';
      botState.textContent=b.online?'● online':'● offline';
      if(!b.online) botState.style.color='var(--red)';

      const wifiAlert=document.createElement('span');
      wifiAlert.className='ip-wifi-alert';
      wifiAlert.textContent='';
      wifiAlert.title=b.ip_duplicate
        ? 'IP compartilhado com '+b.ip_shared_count+' bots'
        : 'IP exclusivo';
      wifiAlert.setAttribute('aria-label',wifiAlert.title);
      wifiAlert.hidden=!b.ip_duplicate;

      stateWrap.append(botState,wifiAlert);
      head.append(botName,stateWrap);

      const ipLine=document.createElement('div');
      ipLine.className='bot-ip'+(b.public_ip?'':' pending');
      ipLine.textContent=b.public_ip ? 'IP • '+b.public_ip : 'IP • aguardando leitura';

      const actions=document.createElement('div');
      actions.className='bot-actions';

      for(const cmd of ['ping','status','logs']){
        const bt=document.createElement('button');
        bt.type='button';
        bt.textContent=cmd;
        bt.addEventListener('click',()=>run(c.name,b.bot,cmd));
        actions.appendChild(bt);
      }

      const more=document.createElement('details');
      more.className='bot-more';
      const summary=document.createElement('summary');
      summary.textContent='•••';
      summary.className='btn ghost';
      const menu=document.createElement('div');
      menu.className='more-menu';

      for(const cmd of ['memory','disk','uptime','internet','public_ip']){
        const bt=document.createElement('button');
        bt.type='button';
        bt.className='btn';
        bt.textContent=cmd;
        bt.addEventListener('click',()=>{
          more.open=false;
          run(c.name,b.bot,cmd);
        });
        menu.appendChild(bt);
      }

      more.append(summary,menu);
      actions.appendChild(more);

      if(b.tor_test){
        const note=document.createElement('div');
        note.className='tor-note';
        const s=b.tor_test_state||{};
        if(s.tor_configured){
          note.textContent='TOR TEST • ATIVO'+(s.public_ip?' • IP '+s.public_ip:'');
        }else if(s.checked_at){
          const setup=s.setup||{};
          note.textContent='TOR TEST • INDISPONÍVEL'+(setup.stage?' • '+setup.stage:'')+(setup.detail?' • '+setup.detail:'')+(s.error?' • '+s.error:'');
        }else{
          note.textContent='TOR TEST • aguardando verificação';
        }
        bot.append(head,ipLine,note,actions);
      }else{
        bot.append(head,ipLine,actions);
      }

      grid.appendChild(bot);
    }

    card.append(top,bulk,grid);
    containersEl.appendChild(card);
  }

  if(!containersEl.children.length){
    containersEl.innerHTML='<div class="bot-no-results"><strong>Nenhum bot encontrado</strong><br>Ajuste a busca ou os filtros.</div>';
  }

  if(filterCount){
    filterCount.textContent=visibleTotal+' bot'+(visibleTotal===1?'':'s')+' visível'+(visibleTotal===1?'':'is');
  }

  updateCollapseButton();
}

async function loadBots(manual=false){
  if(loading) return;
  const value=token.value.trim();
  if(!value){
    authState.textContent='Digite o token para conectar.';
    return;
  }

  loading=true;
  refreshBtn.disabled=true;
  if(manual) authState.textContent='Atualizando...';

  try{
    const r=await fetch('/api/bots',{headers:headers(),cache:'no-store'});
    const j=await r.json();

    if(!r.ok){
      if(r.status===401) throw new Error('Token inválido ou diferente do CONTROLLER_TOKEN do Render.');
      throw new Error(j.detail ? JSON.stringify(j.detail) : JSON.stringify(j));
    }

    data=j.containers||[];
    renderStats();
    rebuildSelectors();
    renderContainers();
    authState.textContent='Conectado • '+j.total_bots+' bots carregados';
  }catch(e){
    authState.textContent='Erro de autenticação/conexão';
    setGlobal(false,'Falha na conexão');
    setOutput(String(e),'erro');
  }finally{
    loading=false;
    refreshBtn.disabled=false;
  }
}

document.getElementById('quickCommands').addEventListener('click',(e)=>{
  const btn=e.target.closest('[data-cmd]');
  if(!btn) return;
  commandInput.value=btn.dataset.cmd;
  commandInput.focus();
});

async function auditIPs(container='all'){
  const label=container==='all'?'todos os containers':container;
  setOutput('Auditando IPs públicos...\nEscopo: '+label+'\n\nConsultando os workers...','executando');

  try{
    const r=await fetch('/api/ip-audit',{
      method:'POST',
      headers:headers(),
      body:JSON.stringify({container})
    });
    const j=await r.json();
    if(!r.ok){
      throw new Error(j.detail ? JSON.stringify(j.detail) : JSON.stringify(j));
    }

    const lines=[];
    lines.push('AUDITORIA DE IP');
    lines.push('Escopo: '+label);
    lines.push('Bots verificados: '+j.successful+'/'+j.checked);
    lines.push('IPs distintos: '+j.distinct_ips);
    lines.push('IPs duplicados: '+j.duplicate_ip_count);
    lines.push('Bots em grupos duplicados: '+j.duplicate_bot_count);

    if(j.duplicate_groups.length){
      lines.push('');
      lines.push('DUPLICADOS');
      for(const group of j.duplicate_groups){
        lines.push('');
        lines.push(group.ip+' • '+group.count+' bots');
        for(const owner of group.bots){
          lines.push('  - '+owner.container+'/'+owner.bot);
        }
      }
    }else{
      lines.push('');
      lines.push('✓ Nenhum IP duplicado encontrado neste escopo.');
    }

    if(j.unique_groups.length){
      lines.push('');
      lines.push('IPs exclusivos: '+j.unique_groups.length);
      for(const group of j.unique_groups){
        const owner=group.bots[0];
        lines.push('  '+group.ip+' → '+owner.container+'/'+owner.bot);
      }
    }

    if(j.failures.length){
      lines.push('');
      lines.push('FALHAS: '+j.failures.length);
      for(const item of j.failures){
        lines.push('  - '+item.container+'/'+item.bot+' → '+item.error);
      }
    }

    setOutput(lines.join('\n'),j.failures.length?'parcial':'concluído');
  }catch(e){
    setOutput(String(e),'erro');
  }
}

const auditAllIps=document.getElementById('auditAllIps');
if(auditAllIps){
  auditAllIps.addEventListener('click',()=>auditIPs('all'));
}

async function runTyped(){
  const raw=commandInput.value.trim();
  if(!raw){
    setOutput('Digite um comando antes de executar.','aguardando');
    return;
  }
  const parts=raw.split(/\s+/);
  const cmd=parts.shift().toLowerCase();
  const allowed=['ping','status','uptime','hostname','disk','memory','echo','logs','internet','public_ip'];
  if(!allowed.includes(cmd)){
    setOutput('Comando não permitido.\n\nPermitidos: '+allowed.join(', '),'bloqueado');
    return;
  }
  let args=parts;
  if(cmd==='logs' && args.length===0) args=['60'];
  await run(containerSel.value,botSel.value,cmd,args);
}

runBtn.addEventListener('click',runTyped);
commandInput.addEventListener('keydown',(e)=>{
  if(e.key==='Enter') runTyped();
});

async function run(container,bot,command,argsOverride=null){
  const args=argsOverride ?? (command==='logs'?[60]:[]);
  runBtn.disabled=true;
  setOutput('> '+command+'\nAlvo: '+container+'/'+bot+'\n\nExecutando...','executando');

  try{
    const r=await fetch('/api/command',{
      method:'POST',
      headers:headers(),
      body:JSON.stringify({container,bot,command,args})
    });
    const j=await r.json();

    if(!r.ok){
      throw new Error(j.detail ? JSON.stringify(j.detail) : JSON.stringify(j));
    }

    if(j.targets!==undefined){
      const summary='Lote concluído • '+j.succeeded+'/'+j.targets+' sucesso(s) • '+j.failed+' falha(s)';
      setOutput(summary+'\n\n'+JSON.stringify(j.results,null,2),j.failed?'parcial':'concluído');
    }else{
      setOutput(JSON.stringify(j,null,2),'concluído');
    }
  }catch(e){
    setOutput(String(e),'erro');
  }finally{
    runBtn.disabled=false;
  }
}

setInterval(()=>{
  if(
    token.value.trim() &&
    (!liveSocket || liveSocket.readyState!==WebSocket.OPEN)
  ){
    loadBots();
  }
},30000);

if(token.value.trim()){
  authState.textContent='Token carregado do navegador';
  loadBots();
  connectLive();
}
</script>
</body>
</html>""")
