# VertraCloud - 20 Bots Python

Projeto para rodar **20 agentes locais** em um container Linux e controlá-los por uma API HTTP autenticada.

## Python

Recomendado: **Python 3.13**.

## Variáveis de ambiente

Configure no painel da VertraCloud:

```text
BOT_API_TOKEN=coloque-uma-chave-grande-e-aleatoria
PORT=8080
```

Não coloque o token diretamente no GitHub.

## Inicialização na VertraCloud

Use:

```bash
python3 start.py
```

O `start.py` inicia:

- 20 bots: `bot-01` até `bot-20`
- 1 controller HTTP em `0.0.0.0:$PORT`

## API externa

Health check (não exige token):

```bash
curl https://SEU-DOMINIO/health
```

Listar os 20 bots:

```bash
curl https://SEU-DOMINIO/bots \
  -H "Authorization: Bearer SEU_TOKEN"
```

Executar em um bot:

```bash
curl -X POST https://SEU-DOMINIO/command \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot":"bot-01","command":"status"}'
```

Executar nos 20:

```bash
curl -X POST https://SEU-DOMINIO/command \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot":"all","command":"ping"}'
```

Exemplo com argumentos:

```bash
curl -X POST https://SEU-DOMINIO/command \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot":"bot-03","command":"echo","args":["ola","mundo"]}'
```

## Comandos permitidos

- `ping`
- `status`
- `uptime`
- `hostname`
- `disk`
- `memory`
- `echo`
- `stop`
- `exec` / `shell` — execução de shell livre no bot (ls, curl, cat, ps, python3, ...)

## Execução de shell (exec)

O comando `exec` executa qualquer comando no shell do bot e herda a rota de IP
daquele bot (proxy/Tor), então `curl` sai pelo IP do bot.

Campos opcionais: `args` (lista), `command_line` (linha completa, preserva
aspas e pipes), `timeout` (default 30s, teto 300s), `cwd`, `stdin` e `shell`.

```bash
curl -X POST https://SEU-DOMINIO/command \n  -H "Authorization: Bearer SEU_TOKEN" \n  -H "Content-Type: application/json" \n  -d '{"bot":"bot-01","command":"exec","command_line":"ls -la && whoami"}'
```

```bash
curl -X POST https://SEU-DOMINIO/command \n  -H "Authorization: Bearer SEU_TOKEN" \n  -H "Content-Type: application/json" \n  -d '{"bot":"bot-01","command":"exec","args":["curl","-s","https://api.ipify.org"],"timeout":60}'
```

A resposta traz `stdout`, `stderr`, `returncode`, `duration_ms` e `route`
(qual proxy/IP o comando usou).

No dashboard, os atalhos `ls -la`, `curl IP`, `whoami`, `df -h` e `ps aux` já
usam `exec`, e o campo `timeout (s)` define o tempo máximo da execução.
