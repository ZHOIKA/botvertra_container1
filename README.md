# VertraCloud - 20 Bots Python

Projeto simples para rodar **20 agentes locais** dentro de um container Linux.

## Python

Recomendado: **Python 3.13**.

> Python 2.13 não existe.

## Arquivos

- `bot.py` — processo do agente
- `start.sh` — inicia os 20 bots
- `stop.sh` — encerra os bots
- `status.sh` — mostra estado/PID
- `send.sh` — envia um comando local
- `read.sh` — lê a última resposta
- `logs/` — logs
- `pids/` — PIDs
- `commands/` — fila local de comandos
- `state/` — estado dos bots

## Instalação

```bash
git clone https://github.com/ZHOIKA/botvertra.git
cd botvertra
chmod +x *.sh
./install.sh
./start.sh
```

## Status

```bash
./status.sh
```

## Enviar comandos

Para um bot:

```bash
./send.sh bot-01 status
./send.sh bot-07 uptime
./send.sh bot-03 echo ola mundo
```

Para todos:

```bash
./send.sh all status
./send.sh all ping
```

Depois:

```bash
./read.sh bot-01
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

O projeto propositalmente **não executa shell arbitrário recebido pela rede**.
Se quiser tarefas específicas, adicione funções explícitas em `ALLOWED_COMMANDS`.

## Observação de segurança

Mesmo que você tenha root no container, é melhor executar os bots como um usuário sem privilégios quando possível.
