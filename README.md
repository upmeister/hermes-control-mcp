# hermes-zcode-bridge

Тонкий MCP stdio bridge для ZCode → Hermes Agent API Server и live TUI gateway.
Он не добавляет новый UI и не меняет Hermes core: ZCode запускает один
долгоживущий MCP процесс, bridge вызывает authenticated `/v1/runs` или один
долгоживущий `/api/ws`, а Hermes сам сохраняет session/run state.

## Что решает Stage 1 и Stage 2

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

Stage 2 добавляет отдельный живой leg, не меняя этот Stage1 fallback:

```text
ZCode MCP stdio ──SSH──> bridge ──persistent WS──> hermes serve /api/ws :9119
                                                   │
                                                   ├─ Hermes Desktop
                                                   └─ dashboard/TUI clients
```

Оба клиента остаются attached к одному Hermes runtime. `session.resume` не
забирает live lease: текущий Hermes `tui_gateway` fan-out'ит events всем
аутентифицированным attachments.

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

Stage 2 live tools:

- `live_session_open` — открыть или resume одну lane через `/api/ws`; результат
  содержит `session_id` (runtime identity) и `stored_session_id` (durable identity);
- `live_prompt` — отправить prompt в существующий TUI session; переносы строк и
  tab сохраняются буквально; `wait_seconds` опционально ждёт terminal event;
- `live_wait` — дождаться пары `message.start` → `message.complete`;
- `live_events` — прочитать bounded in-memory event buffer после `after_seq`;
- `live_status` / `live_history` — recovery reads;
- `live_steer` / `live_interrupt` — exact-session controls;
- `live_reconcile` — проверить неизвестный submit по durable history, не повторяя его;
- `live_reconnect` — новый WS generation + replay retained events;
- `live_health` — auth/connection/replay/buffer health без LLM turn.

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

Для live `prompt.submit` acknowledgement без ответа помечается
`status: "unknown", error_code: "transport_unknown"`. Bridge не отправляет
такой prompt повторно — даже после перезапуска MCP процесса. Сначала вызывается
`live_reconcile`; history match является evidence, но одинаковые повторные
prompts нельзя различить абсолютно.

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
        "/path/to/hermes-control-mcp/scripts/run-bridge.sh",
        "--gateway-url",
        "ws://example.invalid:9119/api/ws",
        "--gateway-access-token-env",
        "HERMES_DASHBOARD_ACCESS_TOKEN"
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

`--gateway-url` — обычная настройка endpoint. Значение
`HERMES_DASHBOARD_ACCESS_TOKEN` должно быть dashboard access token в server-side
`.env` (не `API_SERVER_KEY` и не `HERMES_DASHBOARD_SESSION_TOKEN`). Bridge
отправляет его только на `POST /api/auth/ws-ticket`, получает one-use ticket и
передаёт ticket в `Sec-WebSocket-Protocol`; при каждом reconnect ticket
выпускается заново. Если access token не настроен, live tools возвращают
структурированный `gateway_auth_failed`, а durable API tools продолжают работать.

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
wait, SSE parsing, history, error redaction, persistent TUI WS, event replay,
seq-gap race, ticket/refresh rotation и отсутствие secret в stderr. Настоящий
loopback `websockets 15.0.1` smoke также прошёл с двумя WS generations и
replay seq `[1, 2, 3, 4]`; three strict repetitions and the 30-test suite are
green; LLM-turn smoke намеренно не запускался.

## API Server activation

На текущем сервере Stage 1 activation уже применена и проверена: API Server
слушает только `127.0.0.1:8642`, gateway active, authenticated health/models/
capabilities probes проходят. Для другой установки применяй конфигурацию ниже.

Перед включением нужно добавить в server-side Hermes `.env`:

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

### Stage 2 live authentication

Текущий `hermes-dashboard.service` слушает Tailscale `example.invalid:9119` и
включает OAuth/basic auth gate. Его `HERMES_DASHBOARD_SESSION_TOKEN` — legacy
loopback credential и **не** подходит для gated `/api/ws`; bridge намеренно
получает `HTTP 403`/`gateway_auth_failed`, вместо обхода gate.

Для live leg нужен один из вариантов:

1. `HERMES_DASHBOARD_ACCESS_TOKEN` — dashboard access token; bridge отправляет
   его на существующий `POST /api/auth/ws-ticket` и получает новый одноразовый
   ticket на каждый WS connect/reconnect. При `401/403` bridge может один раз
   вызвать `/auth/native/refresh`, если настроен `HERMES_DASHBOARD_REFRESH_TOKEN`,
   и повторить mint с новым access token;
2. `HERMES_DASHBOARD_REFRESH_TOKEN` — optional native refresh token; rotated
   access/refresh values держатся только в памяти текущего bridge и не пишутся
   обратно в `.env`, поэтому после рестарта нужен действующий env refresh token;
3. `--gateway-ticket-env` — заранее выданный single-use ticket для одной
   сессии (после disconnect требуется новый ticket);
4. legacy `HERMES_DASHBOARD_SESSION_TOKEN` — только для loopback dashboard,
   где auth gate выключен.

`API_SERVER_KEY` относится только к `:8642` и не является заменой dashboard
access token. Если live credential не настроен или отвергнут, Stage1 API tools
остаются usable, а live tools сообщают точную причину.

## Границы Stage 1

API run process и Hermes Desktop `hermes serve` — разные runtime/transport
процессы. Stage 1 даёт durable job lane, status, recovery и control. Stage 2
уже реализует shared live session, attach/reconnect/replay и совместную
очередь prompt через TUI WebSocket; он не создаёт новый agent runtime, а
подключается вторым authenticated client к существующему `/api/ws`. Production
concurrency/LLM gate ждёт operator auth credential. A2A, peer/Bot Chat и public
package release пока backlog.

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
