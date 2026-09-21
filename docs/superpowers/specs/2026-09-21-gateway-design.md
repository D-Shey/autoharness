# Gateway-MCP — дизайн

Дата: 2026-09-21. Статус: на ревью.

## Зачем

Агенты читают MCP-серверы из своего конфига только при старте сессии, поэтому после
`autoharness add mcp` нужен перезапуск. Gateway снимает это ограничение: autoharness сам
становится MCP-сервером, который регистрируется в агенте один раз и дальше запускает
добавленные серверы у себя, без перезапуска агента.

### Почему не только `tools/list_changed`

Протокол MCP позволяет серверу сообщить агенту, что список инструментов изменился. На сентябрь
2026 на это нельзя полагаться:

| Агент | Состояние |
|---|---|
| Claude Code | документация заявляет поддержку с 2.1.0, но новые инструменты не попадают в отложенный поиск инструментов — issue anthropics/claude-code#77314 (2.1.198), закрыт как not planned |
| Codex | не перезапрашивает `tools/list` после уведомления — openai/codex#33266 (0.144.1), открыт |
| Gemini CLI | код влит, но не работает — google-gemini/gemini-cli#19509, открыт, P1 |
| Qwen Code, GigaCode | данных нет; Qwen Code — форк Gemini CLI |

Поэтому основной путь — **неизменный набор мета-инструментов**, которые работают в любом агенте.
Родные инструменты с уведомлением — дополнение там, где агент его обработает.

## Вне рамок

- OAuth-вход для удалённых серверов (такие серверы идут в конфиг агента, как сейчас).
- Устаревший SSE-транспорт (`type: "sse"`) — тоже напрямую в конфиг агента.
- Уведомления от HTTP-серверов (GET-поток Streamable HTTP): gateway их не слушает;
  `list_changed` принимается только от stdio-серверов.
- Запросы дочерних серверов к клиенту (`sampling`, `roots/list`, `elicitation`): ответ
  `-32601 Method not found`.
- Отмена вызовов и прогресс.

## Установка gateway

`autoharness gateway install` (для агента проекта, `--agent` как обычно):

1. Пишет в MCP-конфиг агента сервер с именем `autoharness`:
   - `{"command": "autoharness", "args": ["gateway", "serve"]}`, если `autoharness` есть в `PATH`;
   - иначе `{"command": "<sys.executable>", "args": ["-m", "autoharness", "gateway", "serve"]}`.
   Если сервер `autoharness` уже есть в конфиге и не записан autoharness — ошибка, как для
   любого чужого сервера.
2. Создаёт или обновляет `.autoharness/gateway.json`: `installed` получает имя агента.
3. Ответ: `next` — попросить пользователя перезапустить агента (последний раз) и одобрить
   проектный сервер.

`gateway uninstall` — обратное; серверы из `gateway.json` остаются записанными, но пока gateway
не установлен, `add mcp` снова пишет в конфиг агента.
`gateway status` — установлен ли gateway для агента, какие серверы в `gateway.json`,
ошибки из последнего запуска (если gateway писал `gateway.state.json`, см. ниже).

`.autoharness/.gitignore` пропускает и `gateway.json` (как `config.json` и `kept.json`).

## `.autoharness/gateway.json`

```json
{
  "installed": ["claude"],
  "servers": {
    "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
    "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
               "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}}
  }
}
```

Конфиги — в каноническом виде (формат `.mcp.json`, секреты `${VAR}`), тот же, что в lockfile.
Кто владеет записью, по-прежнему знают lockfile задачи и `kept.json`; `gateway.json` — только
«что должно быть запущено».

## Маршрут `add mcp`

Сервер идёт через gateway, если одновременно:

- `.autoharness/gateway.json` содержит текущего агента в `installed` **и** в конфиге агента есть
  сервер `autoharness`;
- конфиг проксируемый: есть `command` (stdio) **или** `type == "http"` с непустыми `headers`
  (удалённый сервер без заголовков может требовать OAuth — он идёт напрямую);
- не передан `--direct`.

Тогда:

- запись добавляется в `gateway.json["servers"]`, в конфиг агента ничего не пишется;
- запись lockfile получает `"via": "gateway"`;
- ответ `add`: `"via": "gateway"`, `next`: сервер доступен сейчас — вызвать `harness_tools`,
  затем `harness_call` (или родные `<server>__<tool>`, если агент их показал); перезапуск не нужен.
  Если нужны секреты — указать переменные и то, что их должен видеть процесс агента.

Иначе поведение прежнее, а если gateway не установлен, `next` добавляет подсказку: один раз
предложить пользователю `autoharness gateway install`, чтобы следующие серверы не требовали
перезапуска.

`remove_mcp`, `teardown` (`remove`), `Project.remove_kept`, `prune` для записей с
`via: gateway` удаляют сервер из `gateway.json` вместо конфига агента. `keep` оставляет
его в `gateway.json`. Проверка «уже есть и не наш» для имени учитывает и `gateway.json`.

## `gateway serve`

MCP-сервер на stdio (построчный JSON-RPC 2.0, по одному сообщению в строке, UTF-8).

**Поиск проекта.** От текущего каталога вверх до первого, где есть `.autoharness/gateway.json`;
нет — работает с пустым набором и сообщает об этом в `harness_tools`.

**`initialize`.** Ответ: `protocolVersion` — запрошенная агентом, если она в
`SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")`, иначе первая из списка;
`capabilities: {"tools": {"listChanged": true}}`; `serverInfo: {"name": "autoharness", "version"}`;
`instructions`: коротко про `harness_tools` / `harness_call`.

**`tools/list`.** Мета-инструменты, затем родные инструменты всех запущенных серверов.

```json
{"name": "harness_tools",
 "description": "List MCP servers autoharness added for this task and their tools (with input schemas). Call this after `autoharness add mcp`.",
 "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}}}}
{"name": "harness_call",
 "description": "Call a tool of an MCP server autoharness added: harness_call(server, tool, arguments).",
 "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
                 "arguments": {"type": "object"}}, "required": ["server", "tool"]}}
```

Родное имя: `re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]`; при совпадении имён
отдаётся первое по алфавиту серверов, остальные — только через `harness_call`.

**`tools/call`.** `harness_tools` → текст с JSON
`{"servers": {name: {"status": "running|starting|error", "error"?, "transport": "stdio|http",
"tools": [{"name", "native_name"|null, "description", "inputSchema"}]}}, "project": path|null}`.
`harness_call` и родные имена → `tools/call` дочернего сервера, результат (`content`, `isError`,
`structuredContent`) возвращается без изменений. Неизвестный сервер или инструмент →
результат с `isError: true` и текстом, что делать (`harness_tools`).

**`ping`** → `{}`. Прочие методы → `-32601`. Уведомления от агента (`notifications/initialized`,
`notifications/cancelled`) — принимаются молча.

**Слежение.** Поток раз в секунду проверяет `mtime` `gateway.json`. При изменении: новые серверы
запускаются (в фоне, статус `starting`), удалённые останавливаются, изменённые перезапускаются;
затем агенту уходит `notifications/tools/list_changed`. То же — когда stdio-сервер сам прислал
`list_changed` (gateway перечитывает его `tools/list`). Уведомление отправляется только после
того, как агент прислал `notifications/initialized`.

**Состояние для `gateway status`.** После каждого изменения gateway пишет
`.autoharness/gateway.state.json`: `{"pid", "updated", "servers": {name: {"status", "error"?, "tools": n}}}`.

## Дочерние серверы (`mcp_client.py`)

Общий интерфейс: `start()`, `list_tools() -> list[dict]`, `call_tool(name, arguments) -> dict`,
`close()`; при запуске — `initialize` (наша последняя версия из `SUPPORTED`,
`capabilities: {}`, `clientInfo: {"name": "autoharness-gateway"}`) → `notifications/initialized`
→ `tools/list` (с пагинацией по `nextCursor`).

**Подстановка секретов.** `${VAR}` в `command`, `args`, `env`, `url`, `headers` заменяются
значениями из окружения gateway. Нет переменной → сервер получает статус
`error: set <VAR> in the environment the agent is started from`, процесс не запускается.

**stdio.** `subprocess.Popen` с `shutil.which(command)` (на Windows находит `npx.cmd`),
окружение = окружение gateway + `env` конфига, `stdin/stdout` — пайпы, `stderr` —
в `.autoharness/gateway.log`. Поток-читатель разбирает строки: ответы сопоставляются с
ожидающими запросами по `id`, уведомления `notifications/tools/list_changed` вызывают
перечитывание, запросы от сервера получают `-32601`. Нестрочный вывод в stdout дочернего
сервера пишется в лог и пропускается.

**HTTP (Streamable HTTP).** `urllib.request`, `POST url`, заголовки
`Content-Type: application/json`, `Accept: application/json, text/event-stream`, заголовки
конфига, после `initialize` — `MCP-Protocol-Version` и `Mcp-Session-Id` (если сервер его выдал).
Ответ `application/json` — сообщение целиком; `text/event-stream` — читаются события `data:`
до ответа с нашим `id`; `202` на уведомление — ок. `close()` шлёт `DELETE` с `Mcp-Session-Id`,
ошибки игнорируются.

**Таймауты и сбои.** Запрос к дочернему серверу ждёт `AUTOHARNESS_GATEWAY_TIMEOUT` секунд
(по умолчанию 120) → результат `isError: true` «timed out after N s». Упавший stdio-процесс →
статус `error`; следующий вызов один раз пробует перезапустить, при неудаче — `isError: true`
с последними строками лога. Gateway при завершении (EOF на stdin, сигнал) закрывает всех
дочерних.

**stdout gateway** — только JSON-RPC. Всё остальное — в stderr и `.autoharness/gateway.log`.

## Инструкции агенту (`SKILL.md`)

- Шаг 3: если `add mcp` ответил `via: gateway`, сервер доступен сразу: `harness_tools`, затем
  `harness_call` или родные `<server>__<tool>`, если они появились. Перезапуск не нужен.
- Если gateway не установлен и `add mcp` подсказал `gateway install` — предложить пользователю
  один раз, объяснив, что это последний перезапуск.
- Абзац про «New MCP servers load at session start» остаётся для случая без gateway.

README: раздел «Gateway», строка в таблице команд, пункт в «Ограничениях MVP» меняется на
пределы gateway (OAuth и SSE — напрямую; родные инструменты зависят от `list_changed` в агенте).

## Размещение в коде

| Файл | Что |
|---|---|
| `src/autoharness/mcp_client.py` (новый) | `expand_secrets`, `StdioClient`, `HttpClient`, `make_client(name, config, log)` |
| `src/autoharness/gateway.py` (новый) | реестр `gateway.json` (`load`, `save`, `is_installed`, `proxyable`), `Gateway` (JSON-RPC сервер), `serve()` |
| `src/autoharness/env.py` | маршрут в `Task.add_mcp` / `remove_mcp` / `_keep_all` / `Project.remove_kept`, `STATE_GITIGNORE` |
| `src/autoharness/cli.py` | `gateway install|uninstall|status|serve`, `add mcp --direct`, ответы `add` |
| `tests/fake_mcp_server.py` (новый) | фейковый stdio MCP-сервер для тестов |

## Ошибки

| Ситуация | Поведение |
|---|---|
| `gateway install`, а сервер `autoharness` уже есть и он чужой | ошибка «not managed by autoharness» |
| `gateway.json` не парсится | gateway оставляет прежний набор, ошибка в логе и в `harness_tools` (`registry_error`) |
| нет переменной окружения для секрета | статус `error` с именем переменной |
| команда не найдена | статус `error: command not found: <cmd>` |
| дочерний сервер не ответил на `initialize` за таймаут | статус `error`, процесс остановлен |

## Тесты

Без сети и без Node: фейковый stdio-сервер `tests/fake_mcp_server.py` (инструменты `echo`
и `slow`, по команде `add_tool` сам шлёт `list_changed`) и фейковый HTTP-сервер на
`http.server` в потоке (ответы JSON и SSE, `Mcp-Session-Id`).

1. `StdioClient`: initialize, list_tools, call_tool, ответ на запрос от сервера `-32601`,
   `list_changed` от сервера вызывает перечитывание.
2. `HttpClient`: JSON-ответ, SSE-ответ, передача `Mcp-Session-Id`, подстановка `${VAR}` в заголовки.
3. `expand_secrets`: подстановка и ошибка при отсутствующей переменной.
4. Сквозной: `gateway serve` процессом; `initialize` → `tools/list` (два мета-инструмента) →
   запись фейкового сервера в `gateway.json` → приходит `list_changed` → `tools/list` содержит
   `fake__echo` → `harness_call` и прямой `fake__echo` возвращают результат → удаление из
   `gateway.json` → `list_changed`, инструмента нет.
5. Таймаут (`slow`) → `isError: true`; упавший сервер → `error` в `harness_tools`.
6. Маршрут `add mcp`: с установленным gateway (stdio → gateway, http с headers → gateway,
   http без headers → напрямую, `--direct` → напрямую), без gateway → напрямую с подсказкой.
7. `teardown remove` и `prune` удаляют из `gateway.json`; `keep` оставляет.
8. `gateway install` / `uninstall` / `status`; отказ при чужом сервере `autoharness`.

Ручная проверка после реализации: `gateway install` в `D:\Projects\ATH`, перезапуск Claude Code,
`add mcp fetch` без перезапуска; зафиксировать, видит ли Claude Code родные инструменты или
работают только мета-инструменты.
