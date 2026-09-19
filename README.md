# hermes-zcode-bridge

Тонкий MCP stdio bridge для ZCode → Hermes Agent API Server и live TUI gateway.
Он не добавляет новый UI: ZCode запускает один долгоживущий MCP процесс, bridge
использует durable `/v1/runs` и планируемый cooperative local attach к тому же
Hermes Desktop/TUI runtime. ChatGPT/OpenAI Tunnel и Dashboard web-token не входят
в текущую архитектуру.

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

Stage 2 добавляет общий живой leg, не меняя этот Stage1 fallback:

```text
ZCode MCP stdio ──SSH──> bridge ──cooperative local attach──> owner TUI gateway
                                                               │
                                                               ├─ Hermes Desktop
                                                               └─ dashboard/TUI clients
```

Цель — оба клиента остаются attached к одному Hermes runtime. `session.resume`
не забирает live lease: текущий Hermes `tui_gateway` fan-out'ит events всем
attached clients. Stage 2 implementation реализует local attach как явный
opt-in через private owner lease; production enablement остаётся отдельным gate.

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

- `live_session_open` — открыть или resume одну lane через configured cooperative local attach к owner TUI gateway (через отдельный private owner route, не Dashboard `/api/ws`); результат
  содержит `session_id` (runtime identity) и `stored_session_id` (durable identity). Аргумент
  `session_id` здесь — stored/durable ID;
- `live_prompt` — отправить prompt в существующий TUI session; переносы строк и
  tab сохраняются буквально; `wait_seconds` опционально ждёт terminal event;
  при явном `session_id` это runtime ID, поэтому для обычного потока предпочитай `lane`;
- `live_wait` — дождаться completion текущего live prompt. Владение ходом доказывается через
  gateway-side inflight evidence (`session.activate`): ход другого attached клиента с другим
  prompt не возвращается как ответ локального запроса. Если доказать владение нельзя (queued
  submit, отсутствие inflight evidence, degraded replay — усечение/ошибка replay или смена
  replay epoch) — `live_wait` возвращает консервативный `ambiguous_turn` /
  `completion_not_observed` вместо возможного чужого ответа. Фундаментальное исключение
  описано в known limitation ниже: байт-в-байт одинаковый prompt другого writer'а
  неразличим, и его completion в terminal window МОЖЕТ быть возвращён как локальный ответ;
- `live_events` — прочитать bounded in-memory event buffer после `after_seq`;
  при явном `session_id` это runtime ID, поэтому предпочитай `lane`;
- `live_status` / `live_history` — recovery reads; при явном `session_id` ожидают
  runtime ID, для стабильного доступа используй `lane`;
- `live_steer` / `live_interrupt` — exact-session controls; явный `session_id`
  также является runtime ID, поэтому предпочитай `lane`;
- `live_reconcile` — проверить неизвестный submit по durable history, не повторяя его. Перед
  submit фиксируется redacted pre-submit boundary (max user `row_id`); после submit
  кандидаты матчатся только строго после boundary. Старый идентичный prompt не может
  рекомсилить новый submit; несколько одинаковых post-boundary строк дают консервативный
  `ambiguous_history_match`; строки без boundary metadata не рекомсятся никогда;
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
`live_reconcile`: boundary-aware history match является evidence, но одинаковые
post-boundary prompts, которые нельзя различить, остаются консервативно `unknown`.

Атрибуция completion в shared runtime: запрос с ack `streaming` доказывает владение
текущим ходом через inflight snapshot (SHA-256 stripped prompt text); ack `queued` не
доказывается и остаётся консервативным. Байт-в-байт одинаковые prompts от двух writers в
одной session остаются фундаментальным ограничением протокола Hermes (нет server-issued
turn/admission ID, upstream U2). Конкретный небезопасный исход этого known limitation:
если локальный ход завершился, а другой writer в terminal window (или в окне потери
событий при reconnect) успел отправить байт-в-байт тот же prompt, приём completion может
вернуть ответ ЧУЖОГО хода как ответ локального запроса. Bridge устраняет все устранимые
варианты этого класса, но сам класс неустраним без upstream turn-identity seam.

Дополнительные conservative-правила `live_wait`: живой чужой ход в inflight-снапшоте
делает результат консервативным (`completion_not_observed`); приём completion требует
отсутствия inflight-снапшота; retained failed-turn снапшот принимается только с terminal
error-кандидатом (success payload под retained failure — консервативный), а упавший ход
никогда не возвращает `answer` — fallback-текст gateway error-payload попадает в `error`,
не в ответ; reconnect, произошедший ВО ВРЕМЯ блокирующего `live_wait`, инвалидирует proof —
wait перепроверяет connection generation и replay epoch перед приёмом любого кандидата;
truncated/ошибочный replay ИЛИ смена replay epoch (ротация runtime очищает буфер — то же
окно потери событий) переводят `live_wait` в консервативный режим даже при успешном
re-proof, и маркер деградации переживает ротацию runtime id; продолжать ожидание после
re-proof можно только после чистого same-epoch reconnect с полным replay (durable
recovery через `live_reconcile`/`live_history`).

## Подключение ZCode через SSH

Скопированный/установленный на сервере checkout можно подключить как обычный
MCP stdio service. Для Stage 2 используй private owner lease, а не Dashboard
credentials или публичный `/api/ws`:

```json
{
  "mcpServers": {
    "hermes_stage2": {
      "command": "ssh",
      "args": [
        "-T",
        "example-host",
        "/path/to/hermes-control-mcp/scripts/run-bridge.sh",
        "--gateway-owner-lease",
        "/path/to/owner_adapter.json",
        "--state-db",
        "/path/to/bridge-state.db",
        "--log-level",
        "WARNING"
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

Эта конфигурация запускает durable MCP lane без Dashboard credentials. Для
opt-in local attach bridge принимает явный путь lease:

```bash
python -m hermes_zcode_bridge.server --gateway-owner-lease \
  "$HERMES_HOME/runtime/owner_adapter/owner_adapter.json"
```

Owner-side gate `dashboard.owner_adapter.enabled` по умолчанию выключен.
Ни web-token, ни
`HERMES_DASHBOARD_ACCESS_TOKEN`, ни `HERMES_DASHBOARD_REFRESH_TOKEN` в ZCode args
или текущем deployment contract не нужны.

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
seq-gap race, ticket/refresh rotation и отсутствие secret в stderr. Настоящий `websockets 15.0.1` smoke для private UDS owner surface и
bridge→owner attach прошёл; disposable existing-session fixture также прошёл
с `session.resume`, `session.activate`, сохранением исходного Desktop-like
клиента и двумя bridge WS generations. Полный bridge suite: 47 тестов green;
LLM-turn smoke намеренно не запускался.

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

### Stage 2 live access — owner adapter (opt-in; disabled by default)

Owner surface использует отдельный route `/api/owner/ws` на private Unix
socket, подключённый к тому же uvicorn/event loop и `tui_gateway`, что и
обычный Hermes runtime. Это не второй Hermes runtime и не переиспользование
Dashboard `/api/ws`.

Lease содержит только runtime/profile/process identity и endpoint metadata:
`runtime_id`, PID, process-start marker, `profile_home`, socket/lease paths,
route и protocol version. Owner route дополнительно проверяет UDS scope,
private permissions, неизменённый lease и живой PID/start marker. Bridge
проверяет lease/socket identity и передаёт только identity query; token/ticket
из lease или Dashboard config не копируются.

Подтверждено в disposable process fixture: публикация lease, UDS handshake,
`gateway.ready`, `gateway.ping`, rejection wrong identity/TCP bypass, cleanup,
existing-session resume/activate, rebind и reconnect. Owner adapter gate
остаётся выключенным по умолчанию; production service не перезапускался.

Предпочтительная граница — Unix socket с filesystem permissions, owner lease,
PID/liveness и profile fencing. Loopback HTTP допустим только если тот же набор
проверок закрывает admission boundary. `auth_required=false`, legacy `?token=`
и использование `internal_ws_credential` из независимого bridge запрещены.

После `gateway.ready` bridge отправляет `client.capabilities` с
`server_requests=false`: bridge не является интерактивным approval/clarify
клиентом и не должен перехватывать server→client requests у Desktop.

## Границы Stage 1

API run process и Hermes Desktop `hermes serve` — разные runtime/transport
процессы. Stage 1 даёт durable job lane, status, recovery и control. Stage 2
transport/replay core; Stage 2 implementation добавляет доказанный local
cooperative attach вторым client к существующему owner runtime без web token.
Security/integration review для этого scope получил GO; production
concurrency/LLM smoke и owner enablement остаются отдельными opt-in gates.
A2A, peer/Bot Chat и public package release пока backlog.

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
