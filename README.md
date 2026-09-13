# hermes-zcode-bridge

Тонкий MCP stdio bridge для ZCode → Hermes Agent API Server. Он не добавляет
новый UI и не меняет Hermes core: ZCode запускает один долгоживущий MCP
процесс, bridge вызывает authenticated `/v1/runs`, а Hermes сам сохраняет
session/run state.

## Что решает Stage 1

Текущий SSH+`hermes chat --oneshot` создаёт новый CLI/runtime path на каждый
prompt. Здесь процессная схема другая:

```text
ZCode MCP client
    │ one stdio connection (optionally one SSH process)
    ▼
ssh example-host → scripts/run-bridge.sh → hermes-zcode-bridge
                                      │ localhost HTTP + Bearer auth
                                      ▼
                              Hermes API Server :8642
                                      │
                                      ▼
                       durable run + Hermes session history
```

Bridge хранит в локальной SQLite только:

- `lane → session_id`;
- `request_id → run_id/session_id/status`;
- fingerprint запроса и `Idempotency-Key`;
- error code и timestamps.

Raw prompt, bearer token и response body в registry не записываются. Ответы и
история остаются на стороне Hermes API Server.

## Инструменты MCP

MCP host увидит следующие tools (обычно с префиксом имени MCP server):

- `run_start` — старт durable run; принимает `lane`, `prompt`, optional exact
  `session_id`, `model`, `provider`, `instructions`, `request_id`;
- `run_status` — status/answer/error для exact `run_id` или latest request lane;
- `run_wait` — bounded polling до terminal status;
- `run_events` — получить сохранённый SSE event stream, если он ещё доступен;
- `run_stop` — cooperative stop exact run;
- `run_steer` — course correction exact running run;
- `session_history` — bounded oldest-first history exact session;
- `bridge_health` — `/health`, `/v1/models`, `/v1/capabilities`, без LLM turn.

Все tools возвращают JSON envelope:

```json
{
  "ok": true,
  "request_id": "req_example",
  "run_id": "run_...",
  "session_id": "session-...",
  "status": "completed",
  "answer": "...",
  "error_code": null,
  "error": null,
  "replayed": false,
  "artifact_refs": [],
  "commit_refs": []
}
```

## Безопасная retry-механика

1. Новый `run_start` получает caller `request_id` (или bridge генерирует его) и
   один `Idempotency-Key`.
2. Если HTTP acknowledgement потерян, результат помечается `unknown`; bridge
   не создаёт новый key и не повторяет POST сам.
3. Явный повтор того же `request_id` с тем же prompt/fingerprint использует
   прежний key. Hermes API Server возвращает исходный `run_id` либо bridge
   позволяет опросить уже известный run.
4. Другой prompt под тем же request ID или попытка незаметно сменить session
   lane получает структурированный conflict.
5. Provider/model передаются только явно указанными значениями; bridge не
   нормализует alias и не делает silent fallback.

`run_status` после disconnect/restart является recovery authority. SSE — это
наблюдение, а не замена status reconciliation.

## Подключение ZCode через SSH

Скопированный/установленный на сервере checkout можно подключить как обычный
MCP stdio service. Пример без credentials:

```json
{
  "mcpServers": {
    "hermes_bridge": {
      "command": "ssh",
      "args": [
        "example-host",
        "/path/to/hermes-control-mcp/scripts/run-bridge.sh"
      ],
      "timeout": 180,
      "connect_timeout": 30
    }
  }
}
```

`ssh` и bridge живут столько, сколько MCP connection ZCode. Поэтому SSH
handshake и Python/Hermes startup происходят один раз на MCP session, а не на
каждый prompt. При reconnect ZCode создаёт новый процесс; registry и API
idempotency позволяют безопасно продолжить работу.

`run-bridge.sh` выбирает Hermes venv, если он есть, добавляет `src` в
`PYTHONPATH` и передаёт управление `python -m hermes_zcode_bridge.server`.
API key берётся из `API_SERVER_KEY` в окружении или server-side
`$HERMES_HOME/.env`. Key не должен появляться в ZCode args, URL, git или logs.

## Локальный запуск и тесты

```bash
./scripts/test.sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src

# Только parser/entrypoint без API key и LLM:
./scripts/run-bridge.sh --help
```

Тесты используют fake transport и локальный fake HTTP server. Они проверяют
MCP initialize/tools/list/tools/call, explicit session/provider, lane binding,
request fingerprint, same-key reconciliation after timeout, exact stop/steer,
wait, SSE parsing, history, error redaction и отсутствие secret в stderr.
LLM-turn smoke намеренно deferred владельцем проекта.

## API Server activation

Перед live probe нужно включить API Server в server-side Hermes `.env`:

```text
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=[REDACTED]
```

Настоящее значение key уже должно существовать только в protected env store;
не копируйте placeholder из этого README. После изменения `.env` требуется
штатный restart/reload gateway с read-back `/health`, `/v1/models` и
`/v1/capabilities`. Gateway restart может прервать текущие Telegram sessions;
это отдельная operational action, не часть fake tests.

Для ZCode с SSH достаточно loopback bind. Не публикуйте `:8642` наружу и не
передавайте key в URL. Если позже понадобится Tailscale bind, это отдельное
решение с auth/firewall preflight.

## Границы Stage 1

API run process и Hermes Desktop `hermes serve` — разные runtime/transport
процессы. Stage 1 даёт durable job lane, status, recovery и control, но не
обещает, что ZCode увидит live token stream Desktop. Shared live session,
attach/reconnect/replay и совместная очередь prompt — Stage 2 через TUI
WebSocket. A2A, peer/Bot Chat и public package release пока backlog.

Issue `#94017` про повторный provider resolution persisted session остаётся
отдельным Hermes risk. Перед использованием named `custom:*` provider нужно
проверить актуальный upstream workaround/merge; этот bridge не подменяет
provider identity и не скрывает ошибки.

## Rollback

Удалить MCP entry из ZCode config и закрыть stdio process достаточно для
отката клиентского канала. Registry можно оставить для последующего resume;
если его нужно убрать, удаляется только локальный
`~/.local/state/hermes-zcode-bridge/bridge.db` после проверки, что active runs
уже reconciled через API. Hermes sessions/API Server bridge code не удаляет.
