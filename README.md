# AutoHarness

Мета-harness: агент стартует «голым» (только skill `autoharness`), сам определяет,
какие capabilities нужны задаче, находит MCP-серверы и skills, проверяет их,
ставит **только на эту задачу**, решает её и потом всё снимает. Удачные наборы
сохраняются как recipes и переиспользуются.

```
task → init → capabilities → recipe? → search → trust check → add → smoke-test
                    ↑                                                  │
                    └──── capability gap (need) ◄──── execute ◄────────┘
                                                        │
                                  verify → teardown --save-recipe
```

## Установка

```bash
pip install -e .                       # CLI `autoharness` (только stdlib, Python 3.10+)
autoharness install-skill              # skill в ./.claude/skills/autoharness
autoharness install-skill --user       # или глобально в ~/.claude/skills
```

Для установки skills нужен Node.js (`npx skills`), для большинства MCP тоже.

## Команды

| Команда | Что делает |
|---|---|
| `init "<задача>"` | создаёт task-окружение, подсказывает capabilities и похожие recipes |
| `plan "<задача>"` | то же без побочных эффектов |
| `need <capability> --reason ...` | фиксирует capability gap, отдаёт курированных кандидатов |
| `search mcp\|skill <query>` | официальный MCP registry / skills.sh, с trust score |
| `inspect mcp\|skill <name>` | подробный trust-отчёт |
| `add mcp <name>` / `add skill owner/repo@skill` | ставит в `.mcp.json` / `.claude/skills` и пишет в lockfile |
| `remove mcp\|skill <name>` | снимает то, что поставила эта задача |
| `status` | текущий harness и журнал |
| `teardown [--save-recipe NAME --notes ...]` | удаляет всё, что добавила задача |
| `recipe list\|match\|show` | сохранённые наборы |

Всё печатается как JSON. Код выхода `3` = нужно согласие пользователя.

## Безопасность

- **Курированный каталог** (`src/autoharness/data/catalog.json`) — доверенная база.
- **MCP из registry** получают score: проверенный namespace, pinned-версия пакета,
  publisher из trusted-списка. Всё, что не «high», требует секреты или удалённый
  сервер, ставится только с `--approve` после явного «да» от пользователя.
- **Skills** сначала скачиваются во временную staging-папку, статически сканируются
  (чтение `~/.ssh`, `curl | sh`, «не говори пользователю», обфускация) и только
  потом копируются в проект. High-находки блокируют установку.
- **Teardown трогает только то, что записано в lockfile задачи.** Ваши собственные
  MCP и skills не перезаписываются и не удаляются; изменённый после установки skill
  сохраняется в бэкап.
- Секреты не записываются: в конфиге только `${ENV_VAR}`.

Состояние задачи: `.autoharness/tasks/<id>/` (`task.json`, `mcp.lock.json`,
`skills.lock.json`, `permissions.json`, `harness.md`). Recipes:
`~/.autoharness/recipes/` (`AUTOHARNESS_HOME` переопределяет).

## Ограничения MVP

- Новые MCP-серверы Claude Code подхватывает только при старте сессии (или через `/mcp`),
  так что hot-load в середине задачи требует перезапуска. Следующий шаг — gateway-MCP,
  который проксирует динамически добавленные серверы без рестарта.
- Классификация задачи по ключевым словам — лишь подсказка; решает агент.
- Генерация собственных skills, когда подходящего нет, пока не реализована.

## Тесты

```bash
pip install -e .[dev] && pytest
```
