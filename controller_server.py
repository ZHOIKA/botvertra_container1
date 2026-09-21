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

ALLOWED = {
    "ping", "status", "uptime", "hostname",
    "disk", "memory", "echo", "logs"
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
<title>BotVertra</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#090d14;color:#eef;font-family:Arial,sans-serif}
.wrap{max-width:1200px;margin:auto;padding:24px}
.top,.console-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select,button{background:#121a27;color:#eef;border:1px solid #29364b;border-radius:9px;padding:10px}
input{min-width:280px}button{cursor:pointer}
.console{margin-top:18px;background:#0f1622;border:1px solid #223047;border-radius:14px;padding:14px}
.console-row select{min-width:150px}.console-row input{flex:1;min-width:240px}
.hint,.muted{color:#8fa1ba;font-size:12px}
.container{margin-top:24px}.container-head{display:flex;justify-content:space-between;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:10px}
.card{background:#0f1622;border:1px solid #223047;border-radius:14px;padding:14px}
.row{display:flex;justify-content:space-between}.online{color:#60e6a8}.offline{color:#ff718b}
.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}.actions button{font-size:12px;padding:7px}
pre{background:#05080d;border:1px solid #202a3b;border-radius:12px;padding:14px;max-height:460px;overflow:auto;white-space:pre-wrap}
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

<div class="console">
<h3 style="margin-top:0">Console</h3>
<div class="console-row">
<select id="containerSel"></select>
<select id="botSel"></select>
<input id="command" placeholder="Ex.: status | logs 100 | echo oi">
<button onclick="runTyped()">Executar</button>
</div>
<div class="hint">Permitidos: ping, status, uptime, hostname, disk, memory, echo, logs</div>
</div>

<div id="containers"></div>

<h3>Saída</h3>
<pre id="out">Pronto.</pre>
</div>

<script>
const token=document.getElementById('token');
const containersEl=document.getElementById('containers');
const out=document.getElementById('out');
const state=document.getElementById('state');
const containerSel=document.getElementById('containerSel');
const botSel=document.getElementById('botSel');
const commandInput=document.getElementById('command');

let data=[];

function readSavedToken(){
  try{
    return localStorage.getItem('botvertra_token')||'';
  }catch(e){
    return '';
  }
}

function writeSavedToken(value){
  try{
    localStorage.setItem('botvertra_token',value);
    return true;
  }catch(e){
    return false;
  }
}

token.value=readSavedToken();

function headers(){
  return {'Authorization':'Bearer '+token.value.trim(),'Content-Type':'application/json'}
}

function save(){
  const value=token.value.trim();
  if(!value){
    state.textContent='digite o token';
    return;
  }
  const saved=writeSavedToken(value);
  state.textContent=saved ? 'token salvo ✓' : 'token em uso (armazenamento bloqueado)';
  loadBots();
}

function rebuildSelectors(){
  const previousContainer=containerSel.value;
  const previousBot=botSel.value;

  containerSel.innerHTML='';
  const allContainers=document.createElement('option');
  allContainers.value='all';
  allContainers.textContent='TODOS OS CONTAINERS';
  containerSel.appendChild(allContainers);
  for(const c of data){
    const opt=document.createElement('option');
    opt.value=c.name;
    opt.textContent=c.name+(c.online?'':' (offline)');
    containerSel.appendChild(opt);
  }

  if(previousContainer && [...containerSel.options].some(o=>o.value===previousContainer)){
    containerSel.value=previousContainer;
  }

  const selected=data.find(c=>c.name===containerSel.value);
  botSel.innerHTML='';
  const allBots=document.createElement('option');
  allBots.value='all';
  allBots.textContent=containerSel.value==='all' ? 'TODOS OS BOTS' : 'TODOS DESTE CONTAINER';
  botSel.appendChild(allBots);
  if(selected){
    for(const b of selected.bots){
      const opt=document.createElement('option');
      opt.value=b.bot;
      opt.textContent=b.bot;
      botSel.appendChild(opt);
    }
  }

  if(previousBot && [...botSel.options].some(o=>o.value===previousBot)){
    botSel.value=previousBot;
  }
}

containerSel.addEventListener('change',rebuildSelectors);

async function loadBots(){
  try{
    const r=await fetch('/api/bots',{headers:headers()});
    const j=await r.json();
    if(!r.ok){
      if(r.status===401) throw new Error('Token inválido ou diferente do CONTROLLER_TOKEN do Render.');
      throw new Error(JSON.stringify(j));
    }

    data=j.containers||[];
    state.textContent=j.total_containers+' containers • '+j.total_bots+' bots';
    rebuildSelectors();

    containersEl.innerHTML='';
    for(const c of data){
      const section=document.createElement('section');
      section.className='container';

      const head=document.createElement('div');
      head.className='container-head';
      head.innerHTML='<h3>'+c.name+'</h3><span class="'+(c.online?'online':'offline')+'">'+(c.online?'ONLINE':'OFFLINE')+'</span>';
      const allActions=document.createElement('div');
      allActions.className='actions';
      for(const cmd of ['ping','status','uptime','memory','disk']){
        const bt=document.createElement('button');
        bt.textContent=cmd+' em todos';
        bt.onclick=()=>run(c.name,'all',cmd);
        allActions.appendChild(bt);
      }
      section.appendChild(head);
      section.appendChild(allActions);

      const grid=document.createElement('div');
      grid.className='grid';

      for(const b of c.bots){
        const card=document.createElement('div');
        card.className='card';
        card.innerHTML='<div class="row"><b>'+b.bot+'</b><span class="'+(b.online?'online':'offline')+'">'+(b.online?'ONLINE':'OFFLINE')+'</span></div><div class="actions"></div>';

        const actions=card.querySelector('.actions');
        for(const cmd of ['ping','status','uptime','memory','disk','logs']){
          const bt=document.createElement('button');
          bt.textContent=cmd;
          bt.onclick=()=>run(c.name,b.bot,cmd);
          actions.appendChild(bt);
        }
        grid.appendChild(card);
      }

      section.appendChild(grid);
      containersEl.appendChild(section);
    }
  }catch(e){
    state.textContent='erro/autenticação';
    out.textContent=String(e);
  }
}

async function runTyped(){
  const raw=commandInput.value.trim();
  if(!raw){out.textContent='Digite um comando.';return;}

  const parts=raw.split(/\s+/);
  const cmd=parts.shift().toLowerCase();
  const allowed=['ping','status','uptime','hostname','disk','memory','echo','logs'];

  if(!allowed.includes(cmd)){
    out.textContent='Comando não permitido. Use: '+allowed.join(', ');
    return;
  }

  let args=parts;
  if(cmd==='logs' && args.length===0) args=['60'];

  await run(containerSel.value,botSel.value,cmd,args);
}

commandInput.addEventListener('keydown',e=>{
  if(e.key==='Enter') runTyped();
});

async function run(container,bot,command,argsOverride=null){
  out.textContent='Executando '+command+' em '+container+'/'+bot+'...';
  const args=argsOverride ?? (command==='logs'?[60]:[]);

  try{
    const r=await fetch('/api/command',{
      method:'POST',
      headers:headers(),
      body:JSON.stringify({container,bot,command,args})
    });
    const j=await r.json();
    if(j.targets!==undefined){
      out.textContent='Lote concluído: '+j.succeeded+'/'+j.targets+' sucesso(s), '+j.failed+' falha(s)\n\n'+JSON.stringify(j.results,null,2);
    }else{
      out.textContent=JSON.stringify(j,null,2);
    }
  }catch(e){
    out.textContent=String(e);
  }
}

setInterval(loadBots,5000);
if(token.value) loadBots();
</script>
</body></html>""")

