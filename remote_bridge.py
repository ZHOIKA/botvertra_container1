#!/usr/bin/env python3
import asyncio
import json
import os
import time
from pathlib import Path

import websockets

BASE_DIR = Path(__file__).resolve().parent
CMD_DIR = BASE_DIR / "commands"
STATE_DIR = BASE_DIR / "state"

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "").strip()
CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "").strip()
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "container1").strip()

if not CONTROLLER_URL or not CONTROLLER_TOKEN:
    raise SystemExit("CONTROLLER_URL e CONTROLLER_TOKEN precisam estar configurados")

BOTS = [f"bot-{i:02d}" for i in range(1, 21)]

async def run_local(bot, command, args):
    inbox = CMD_DIR / f"{bot}.json"
    outbox = CMD_DIR / f"{bot}.out.json"

    outbox.unlink(missing_ok=True)
    inbox.write_text(
        json.dumps({"command": command, "args": args}, indent=2),
        encoding="utf-8",
    )

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if outbox.exists():
            try:
                return json.loads(outbox.read_text(encoding="utf-8"))
            except Exception as exc:
                return {"ok": False, "bot": bot, "error": f"invalid_local_response: {exc}"}
        await asyncio.sleep(0.1)

    return {"ok": False, "bot": bot, "error": "local_timeout"}

async def connect_once():
    async with websockets.connect(
        CONTROLLER_URL,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=2_000_000,
    ) as ws:
        await ws.send(json.dumps({
            "type": "auth",
            "token": CONTROLLER_TOKEN,
            "container": CONTAINER_NAME,
            "bots": BOTS,
        }))

        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not auth.get("ok"):
            raise RuntimeError("controller recusou autenticacao")

        print(f"[bridge] conectado • {len(BOTS)} bots registrados", flush=True)

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "command":
                continue

            request_id = msg.get("id")
            bot = msg.get("bot")
            command = msg.get("command")
            args = msg.get("args", [])

            if bot not in BOTS:
                result = {"ok": False, "bot": bot, "error": "unknown_bot"}
            elif not (STATE_DIR / f"{bot}.json").exists():
                result = {"ok": False, "bot": bot, "error": "bot_offline"}
            else:
                result = await run_local(bot, command, args)

            await ws.send(json.dumps({
                "type": "result",
                "id": request_id,
                "bot": bot,
                "result": result,
            }))

async def main():
    delay = 2
    while True:
        try:
            await connect_once()
            delay = 2
        except Exception as exc:
            print(f"[bridge] desconectado: {exc} • retry {delay}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

if __name__ == "__main__":
    asyncio.run(main())
