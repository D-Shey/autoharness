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
autoharness install-skill              # skill для текущего агента в папку проекта
autoharness --agent codex install-skill --user   # глобально для конкретного агента
autoharness install-skill --all        # для всех поддерживаемых агентов
```

## Агенты

| `--agent` | MCP-конфиг проекта | Skills проекта |
|---|---|---|
| `claude` (Claude Code) | `.mcp.json` | `.claude/skills` |
| `codex` (Codex CLI) | `.codex/config.toml` | `.agents/skills` |
| `gemini` (Gemini CLI) | `.gemini/settings.json` | `.gemini/skills` |
| `qwen` (Qwen Code) | `.qwen/settings.json` | `.qwen/skills` |
| `gigacode` (GigaCode CLI) | `.gigacode/settings.json` | `.gigacode/skills` |

Агент выбирается при `init` и запоминается в задаче. Порядок: `--agent`, затем
`$AUTOHARNESS_AGENT`, затем автоопределение по окружению (`CLAUDECODE`, `GEMINI_CLI`, …),
иначе `claude`. `autoharness agents` показывает таблицу и обнаруженного агента.

Lockfiles и recipes хранят конфиг в одном каноническом виде (формат `.mcp.json`,
секреты как `${VAR}`), адаптер переводит его при записи: для Gemini-семейства
remote-серверы становятся `httpUrl`/`url`, для Codex секреты уходят в `env_vars`,
`bearer_token_env_var`, `env_http_headers`. Codex не раскрывает `${VAR}` в `args`;
в этом случае `add` возвращает `warnings`. Проектный `.codex/config.toml` Codex читает
только для trusted-проектов. Правки в TOML обёрнуты маркерами `# >>> autoharness:<name>`,
так что комментарии и остальной конфиг пользователя не трогаются.

Для установки skills нужен Node.js (`npx skills`), для большинства MCP тоже.

## Команды

| Команда | Что делает |
|---|---|
| `init "<задача>"` | создаёт task-окружение, подсказывает capabilities и похожие recipes |
| `plan "<задача>"` | то же без побочных эффектов |
| `need <capability> --reason ...` | фиксирует capability gap, отдаёт курированных кандидатов |
| `search mcp\|skill <query>` | официальный MCP registry / skills.sh, с trust score |
| `inspect mcp\|skill <name>` | подробный trust-отчёт |
| `add mcp <name>` / `add skill owner/repo@skill` | ставит в MCP-конфиг и папку skills агента и пишет в lockfile |
| `remove mcp\|skill <name>` | снимает то, что поставила эта задача |
| `status` | текущий harness и журнал |
| `teardown [--save-recipe NAME --notes ...]` | удаляет всё, что добавила задача |
| `recipe list\|match\|show` | сохранённые наборы |
| `agents` | поддерживаемые агенты и их пути |

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

- Новые MCP-серверы агенты подхватывают только при старте сессии (Claude Code ещё через `/mcp`),
  так что hot-load в середине задачи требует перезапуска. Следующий шаг — gateway-MCP,
  который проксирует динамически добавленные серверы без рестарта.
- Классификация задачи по ключевым словам — лишь подсказка; решает агент.
- Генерация собственных skills, когда подходящего нет, пока не реализована.

## Тесты

```bash
pip install -e .[dev] && pytest
```
