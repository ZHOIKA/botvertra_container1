#!/usr/bin/env python3
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CMD_DIR = BASE_DIR / "commands"
STATE_DIR = BASE_DIR / "state"
PID_DIR = BASE_DIR / "pids"

for directory in (CMD_DIR, STATE_DIR, PID_DIR):
    directory.mkdir(exist_ok=True)

TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))
ALLOWED_COMMANDS = {"ping", "status", "uptime", "hostname", "disk", "memory", "echo", "stop"}

if not TOKEN:
    raise SystemExit("BOT_API_TOKEN nao configurado. Defina a variavel no painel da VertraCloud.")

def valid_bot(bot):
    if bot == "all":
        return True
    if not isinstance(bot, str) or not bot.startswith("bot-"):
        return False
    try:
        n = int(bot.split("-", 1)[1])
    except (ValueError, IndexError):
        return False
    return 1 <= n <= 20 and bot == f"bot-{n:02d}"

def write_command(bot, command, args):
    payload = {"command": command, "args": args}
    path = CMD_DIR / f"{bot}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def wait_response(bot, timeout=5.0):
    out = CMD_DIR / f"{bot}.out.json"
    start = time.time()
    old_mtime = out.stat().st_mtime if out.exists() else 0

    while time.time() - start < timeout:
        if out.exists() and out.stat().st_mtime > old_mtime:
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except Exception as exc:
                return {"ok": False, "bot": bot, "error": f"invalid_response: {exc}"}
        time.sleep(0.1)

    return {"ok": False, "bot": bot, "error": "timeout_waiting_for_bot"}

def get_bots():
    result = []
    for i in range(1, 21):
        bot = f"bot-{i:02d}"
        state_file = STATE_DIR / f"{bot}.json"
        pid_file = PID_DIR / f"{bot}.pid"
        item = {"bot": bot, "online": state_file.exists()}
        if pid_file.exists():
            try:
                item["pid"] = int(pid_file.read_text(encoding="utf-8").strip())
            except Exception:
                pass
        result.append(item)
    return result

class Handler(BaseHTTPRequestHandler):
    server_version = "BotVertraController/1.0"

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        return secrets.compare_digest(auth, expected)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "botvertra-controller"})
            return

        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        if self.path == "/bots":
            self._json(200, {"ok": True, "bots": get_bots()})
            return

        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        if self.path != "/command":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("invalid_content_length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"invalid_json: {exc}"})
            return

        bot = body.get("bot")
        command = str(body.get("command", "")).strip().lower()
        args = body.get("args", [])

        if not valid_bot(bot):
            self._json(400, {"ok": False, "error": "invalid_bot"})
            return

        if command not in ALLOWED_COMMANDS:
            self._json(400, {
                "ok": False,
                "error": "command_not_allowed",
                "allowed": sorted(ALLOWED_COMMANDS),
            })
            return

        if not isinstance(args, list):
            self._json(400, {"ok": False, "error": "args_must_be_list"})
            return

        if bot == "all":
            responses = {}
            for i in range(1, 21):
                target = f"bot-{i:02d}"
                write_command(target, command, args)
            for i in range(1, 21):
                target = f"bot-{i:02d}"
                responses[target] = wait_response(target)
            self._json(200, {"ok": True, "responses": responses})
            return

        write_command(bot, command, args)
        response = wait_response(bot)
        self._json(200 if response.get("ok") else 504, response)

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} - {fmt % args}", flush=True)

if __name__ == "__main__":
    print(f"[api] controller ouvindo em {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
