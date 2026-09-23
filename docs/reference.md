# Справочник

Как всё это складывается в цикл — в [README](../README.md#как-это-работает).

Все команды печатают JSON. Код выхода `3` значит: нужно согласие пользователя. `--agent` и
`--project` — глобальные флаги, пишутся до подкоманды.

## Команды

| Команда | Что делает |
|---|---|
| `init "<задача>"` | создаёт task-окружение, подсказывает capabilities, recipes и находки из реестров |
| `plan "<задача>"` | то же без побочных эффектов |
| `need <capability> --reason ...` | фиксирует capability gap, отдаёт курированных кандидатов и свои skills |
| `search mcp\|skill <query>` | официальный MCP registry / skills.sh, с trust score |
| `inspect mcp\|skill <name>` | подробный trust-отчёт; для MCP ещё `egress` — куда сервер может ходить |
| `brief --goal ... --constraints ... [--review critic]` | план задачи: цель, входы, ограничения, проверка, вердикты по кандидатам |
| `vet mcp\|skill <name> [--answer-file F]` | промпт для независимой проверки кандидата против ограничений; запись ответа |
| `add mcp <name>` / `add skill owner/repo@skill` | ставит в MCP-конфиг (или gateway) и папку skills агента, пишет в lockfile |
| `add mcp <name> --direct` | мимо gateway, сразу в конфиг агента |
| `remove mcp\|skill <name>` | снимает то, что поставила эта задача или сохранила в проекте прошлая |
| `prune` | снимает всё, что прошлые задачи сохранили в проекте |
| `evidence add <file> --kind reference\|result` / `evidence diff [--base REV]` | референс или результат раунда; для кода — весь diff |
| `review` / `critic` | раунд проверки; промпт для независимого критика |
| `approach` / `arbiter` / `arbitrate` / `check` | смена подхода к части, которая не проходит review; арбитр и его проверка |
| `teardown [--keep\|--remove] [--save-recipe NAME --notes ... --query kind:q] [--accepted ...]` | завершает задачу |
| `status` | текущий harness, что сохранено в проекте, журнал |
| `skill new\|save\|list\|delete` | свои skills агента и локальная библиотека |
| `recipe list\|match\|show` | сохранённые наборы; `show` перезапускает запросы recipe и отдаёт новое в `fresh` |
| `config get\|set [teardown keep\|remove \| allowed_hosts "<хосты>"] [--global]` | настройки |
| `gateway install\|uninstall\|status\|serve\|approve` | MCP-сервер autoharness, через который новые серверы работают без перезапуска |
| `install-skill` / `install-hook` / `waive` | установка skill и хука; пропуск autoharness в сессии по слову пользователя |
| `agents` | поддерживаемые агенты и их пути |

## Где что лежит

| Путь | Что там |
|---|---|
| `.autoharness/tasks/<id>/` | состояние задачи: `task.json`, `mcp.lock.json`, `skills.lock.json`, `permissions.json`, `harness.md`, evidence, ответы проверяющих |
| `.autoharness/kept.json`, `config.json`, `gateway.json` | что осталось в проекте, настройки проекта, серверы gateway; их можно коммитить (`.autoharness/.gitignore` пропускает только их), остальное состояние в git не попадает |
| `.autoharness/gateway.log` | лог gateway |
| `~/.autoharness/` | `recipes/`, библиотека своих skills `skills/`, глобальный `config.json`, одобрения gateway (`AUTOHARNESS_HOME` переопределяет) |
| `src/autoharness/data/catalog.json` | курированный каталог capabilities, серверов и доверенных издателей |
| `src/autoharness/skill/SKILL.md` | инструкция для агента, которую ставит `install-skill` |
