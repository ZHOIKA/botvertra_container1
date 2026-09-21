#!/usr/bin/env python3
import asyncio
import json
import os
import secrets
import time
import uuid
import urllib.request

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

TOKEN = os.getenv("CONTROLLER_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("CONTROLLER_TOKEN ausente")

app = FastAPI(title="BotVertra Controller")

@app.on_event("startup")
async def trigger_vertra_deploy_once():
    url = os.getenv("VERTRA_DEPLOY_WEBHOOK", "").strip()
    enabled = os.getenv("TRIGGER_VERTRA_DEPLOY", "0").strip() == "1"
    if not url or not enabled:
        return
    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json", "User-Agent": "botvertra-controller/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"[vertra] deploy webhook HTTP {resp.status}", flush=True)
    except Exception as exc:
        print(f"[vertra] deploy webhook falhou: {exc}", flush=True)

agent_ws = None
agent_bots = set()
agent_name = None
last_seen = 0.0
pending = {}
send_lock = asyncio.Lock()

ALLOWED = {
    "ping", "status", "uptime", "hostname",
    "disk", "memory", "echo", "logs"
}

def check_auth(value):
    expected = f"Bearer {TOKEN}"
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="unauthorized")

class Command(BaseModel):
    bot: str
    command: str
    args: list = []

@app.get("/health")
async def health():
    return {
        "ok": True,
        "agent_connected": agent_ws is not None,
        "agent": agent_name,
        "bots": len(agent_bots),
    }

@app.get("/api/bots")
async def bots(authorization: str | None = Header(default=None)):
    check_auth(authorization)
    connected = agent_ws is not None
    return {
        "ok": True,
        "agent": agent_name,
        "last_seen": last_seen,
        "bots": [{"bot": b, "online": connected} for b in sorted(agent_bots)],
    }

@app.post("/api/command")
async def command(data: Command, authorization: str | None = Header(default=None)):
    check_auth(authorization)

    if data.command not in ALLOWED:
        raise HTTPException(status_code=400, detail={
            "error": "command_not_allowed",
            "allowed": sorted(ALLOWED),
        })

    if data.bot not in agent_bots:
        raise HTTPException(status_code=404, detail="bot_not_registered")

    if agent_ws is None:
        raise HTTPException(status_code=503, detail="container_offline")

    request_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future

    payload = {
        "type": "command",
        "id": request_id,
        "bot": data.bot,
        "command": data.command,
        "args": data.args,
    }

    try:
        async with send_lock:
            await agent_ws.send_text(json.dumps(payload))
        return await asyncio.wait_for(future, timeout=12)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="bot_timeout")
    finally:
        pending.pop(request_id, None)

@app.websocket("/ws/agent")
async def agent(websocket: WebSocket):
    global agent_ws, agent_bots, agent_name, last_seen

    await websocket.accept()

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        auth = json.loads(raw)

        if auth.get("type") != "auth":
            await websocket.close(code=4401)
            return

        received = str(auth.get("token", ""))
        if not secrets.compare_digest(received, TOKEN):
            await websocket.send_text(json.dumps({"ok": False}))
            await websocket.close(code=4401)
            return

        agent_ws = websocket
        agent_bots = set(auth.get("bots", []))
        agent_name = str(auth.get("container", "container1"))
        last_seen = time.time()

        await websocket.send_text(json.dumps({"ok": True}))
        print(f"[agent] {agent_name} conectado com {len(agent_bots)} bots", flush=True)

        while True:
            raw = await websocket.receive_text()
            last_seen = time.time()
            msg = json.loads(raw)

            if msg.get("type") == "result":
                future = pending.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg.get("result", msg))

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if agent_ws is websocket:
            agent_ws = None
            print("[agent] desconectado", flush=True)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse("""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BotVertra</title>
<style>
body{margin:0;background:#090d14;color:#eef;font-family:Arial,sans-serif}
.wrap{max-width:1100px;margin:auto;padding:24px}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,button{background:#121a27;color:#eef;border:1px solid #29364b;border-radius:9px;padding:10px}
input{min-width:300px}button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:20px}
.card{background:#0f1622;border:1px solid #223047;border-radius:14px;padding:14px}
.row{display:flex;justify-content:space-between}.online{color:#60e6a8}.offline{color:#ff718b}
.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}.actions button{font-size:12px;padding:7px}
pre{background:#05080d;border:1px solid #202a3b;border-radius:12px;padding:14px;max-height:420px;overflow:auto}
</style>
</head>
<body><div class="wrap">
<div class="top">
<h2>BotVertra Controller</h2>
<input id="token" type="password" placeholder="CONTROLLER_TOKEN">
<button onclick="save()">Salvar token</button>
<button onclick="loadBots()">Atualizar</button>
<span id="state"></span>
</div>
<div id="grid" class="grid"></div>
<h3>Saída</h3>
<pre id="out">Pronto.</pre>
</div>
<script>
const token=document.getElementById('token');
const grid=document.getElementById('grid');
const out=document.getElementById('out');
const state=document.getElementById('state');
token.value=localStorage.getItem('botvertra_token')||'';

function headers(){return {'Authorization':'Bearer '+token.value,'Content-Type':'application/json'}}
function save(){localStorage.setItem('botvertra_token',token.value);loadBots()}

async function loadBots(){
  try{
    const r=await fetch('/api/bots',{headers:headers()});
    const j=await r.json();
    if(!r.ok) throw new Error(JSON.stringify(j));
    state.textContent=j.agent ? j.agent+' conectado' : 'container offline';
    grid.innerHTML='';
    for(const b of j.bots){
      const card=document.createElement('div');
      card.className='card';
      card.innerHTML='<div class="row"><b>'+b.bot+'</b><span class="'+(b.online?'online':'offline')+'">'+(b.online?'ONLINE':'OFFLINE')+'</span></div><div class="actions"></div>';
      const actions=card.querySelector('.actions');
      for(const cmd of ['ping','status','uptime','memory','disk','logs']){
        const bt=document.createElement('button');
        bt.textContent=cmd;
        bt.onclick=()=>run(b.bot,cmd);
        actions.appendChild(bt);
      }
      grid.appendChild(card);
    }
  }catch(e){
    state.textContent='erro/autenticação';
    out.textContent=String(e);
  }
}

async function run(bot,command){
  out.textContent='Executando '+command+' em '+bot+'...';
  const args=command==='logs'?[60]:[];
  const r=await fetch('/api/command',{
    method:'POST',
    headers:headers(),
    body:JSON.stringify({bot,command,args})
  });
  const j=await r.json();
  out.textContent=JSON.stringify(j,null,2);
}

setInterval(loadBots,5000);
if(token.value) loadBots();
</script>
</body></html>""")
