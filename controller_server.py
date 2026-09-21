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

async def delayed_selftest():
    await asyncio.sleep(25)
    await run_selftest("25s")
    await asyncio.sleep(40)
    await run_selftest("65s")
    await asyncio.sleep(25)
    await run_internet_selftest()

@app.on_event("startup")
async def start_selftest():
    asyncio.create_task(delayed_selftest())
ALLOWED = {
    "ping", "status", "uptime", "hostname",
    "disk", "memory", "echo", "logs", "internet"
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

def snapshot():
    result = []
    for name in sorted(agents):
        item = agents[name]
        connected = item.get("ws") is not None
        result.append({
            "name": name,
            "online": connected,
            "last_seen": item.get("last_seen", 0),
            "bots": [
                {"bot": bot, "online": connected}
                for bot in sorted(item.get("bots", set()))
            ],
        })
    return result

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
        return {"ok": False, "container": container_name, "bot": bot, "error": "bot_timeout"}
    except Exception as exc:
        return {"ok": False, "container": container_name, "bot": bot, "error": str(exc)}
    finally:
        pending.pop(request_id, None)

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
.bot-status{font-size:10px;color:var(--green)}
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
    <div class="stat"><div class="label">Atualização</div><div id="statRefresh" class="value" style="font-size:16px;margin-top:9px">—</div><div class="sub">auto a cada 5s</div></div>
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

  <section class="panel console">
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
      <button class="chip" data-cmd="ping" type="button">ping</button>
      <button class="chip" data-cmd="status" type="button">status</button>
      <button class="chip" data-cmd="uptime" type="button">uptime</button>
      <button class="chip" data-cmd="memory" type="button">memory</button>
      <button class="chip" data-cmd="disk" type="button">disk</button>
      <button class="chip" data-cmd="hostname" type="button">hostname</button>
      <button class="chip" data-cmd="logs 40" type="button">logs 40</button>
      <button class="chip" data-cmd="internet" type="button">internet Google</button>
    </div>
  </section>

  <main class="content-grid">
    <section>
      <div class="section-head">
        <div>
          <h2>Infraestrutura</h2>
          <p id="infraSubtitle">Aguardando dados dos containers.</p>
        </div>
      </div>
      <div id="containers" class="containers">
        <div class="empty"><strong>Nenhum dado carregado</strong>Salve o token para consultar os containers.</div>
      </div>
    </section>

    <aside class="panel output">
      <div class="output-head">
        <h2>Saída do comando</h2>
        <span id="outputStatus" class="output-status">pronto</span>
      </div>
      <pre id="out" class="terminal">BotVertra pronto.</pre>
    </aside>
  </main>

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
const globalDot = document.getElementById('globalDot');
const globalText = document.getElementById('globalText');
const infraSubtitle = document.getElementById('infraSubtitle');
const statContainers = document.getElementById('statContainers');
const statContainersSub = document.getElementById('statContainersSub');
const statBots = document.getElementById('statBots');
const statBotsSub = document.getElementById('statBotsSub');
const statOnline = document.getElementById('statOnline');
const statRefresh = document.getElementById('statRefresh');

let data = [];
let loading = false;

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
function setOutput(text,status='pronto'){
  out.textContent=text;
  outputStatus.textContent=status;
  out.scrollTop=0;
}
function fmtTime(ts){
  if(!ts) return 'sem atividade';
  try{return new Date(ts*1000).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return '—'}
}
function nowLabel(){
  return new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
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
});

refreshBtn.addEventListener('click',()=>loadBots(true));

clearBtn.addEventListener('click',()=>{
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
containerSel.addEventListener('change',rebuildSelectors);

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
  statRefresh.textContent=totalContainers?nowLabel():'—';

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
    return;
  }

  for(const c of data){
    const card=document.createElement('article');
    card.className='panel container-card';

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
    const meta=document.createElement('div');
    meta.className='container-meta';
    meta.textContent=c.bots.length+' bots • última atividade '+fmtTime(c.last_seen);
    texts.append(name,meta);
    titleWrap.append(icon,texts);

    const badge=document.createElement('span');
    badge.className='badge '+(c.online?'online':'offline');
    badge.textContent=(c.online?'● ONLINE':'● OFFLINE');
    top.append(titleWrap,badge);

    const bulk=document.createElement('div');
    bulk.className='bulk-actions';
    for(const cmd of ['ping','status','uptime','memory','disk','internet']){
      bulk.appendChild(actionButton(cmd+' em todos',()=>run(c.name,'all',cmd)));
    }

    const grid=document.createElement('div');
    grid.className='bots-grid';

    for(const b of c.bots){
      const bot=document.createElement('div');
      bot.className='bot';

      const head=document.createElement('div');
      head.className='bot-head';

      const botName=document.createElement('span');
      botName.className='bot-name';
      botName.textContent=b.bot;

      const botState=document.createElement('span');
      botState.className='bot-status';
      botState.textContent=b.online?'● online':'● offline';
      if(!b.online) botState.style.color='var(--red)';

      head.append(botName,botState);

      const actions=document.createElement('div');
      actions.className='bot-actions';
      for(const cmd of ['ping','status','memory','disk','uptime','internet','logs']){
        const bt=document.createElement('button');
        bt.type='button';
        bt.textContent=cmd;
        bt.addEventListener('click',()=>run(c.name,b.bot,cmd));
        actions.appendChild(bt);
      }

      bot.append(head,actions);
      grid.appendChild(bot);
    }

    card.append(top,bulk,grid);
    containersEl.appendChild(card);
  }
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

async function runTyped(){
  const raw=commandInput.value.trim();
  if(!raw){
    setOutput('Digite um comando antes de executar.','aguardando');
    return;
  }
  const parts=raw.split(/\s+/);
  const cmd=parts.shift().toLowerCase();
  const allowed=['ping','status','uptime','hostname','disk','memory','echo','logs','internet'];
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
  if(token.value.trim()) loadBots();
},5000);

if(token.value.trim()){
  authState.textContent='Token carregado do navegador';
  loadBots();
}
</script>
</body>
</html>""")
