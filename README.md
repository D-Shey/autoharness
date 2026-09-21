# AutoHarness

Мета-harness: агент стартует «голым» (только skill `autoharness`), сам определяет,
какие capabilities нужны задаче, находит MCP-серверы и skills, проверяет их,
ставит их, решает задачу и в конце либо оставляет поставленное в проекте (по умолчанию),
либо всё снимает — это настраивается. Удачные наборы сохраняются как recipes и
переиспользуются.

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
| `remove mcp\|skill <name>` | снимает то, что поставила эта задача или сохранила в проекте прошлая |
| `prune` | снимает всё, что прошлые задачи сохранили в проекте |
| `status` | текущий harness, что сохранено в проекте, журнал |
| `teardown [--keep\|--remove] [--save-recipe NAME --notes ...]` | завершает задачу по политике `teardown` |
| `config get\|set [teardown keep\|remove] [--global]` | настройки |
| `skill new\|save\|list\|delete` | свои skills агента и локальная библиотека |
| `recipe list\|match\|show` | сохранённые наборы |
| `agents` | поддерживаемые агенты и их пути |

Всё печатается как JSON. Код выхода `3` = нужно согласие пользователя.

## Что происходит в конце задачи

Настройка `teardown`:

- `keep` (по умолчанию) — поставленное остаётся в проекте. Записи о нём переходят из
  lockfile задачи в `.autoharness/kept.json`, следующие задачи видят эти skills и MCP
  как уже установленные (`add` отвечает `already_installed`). Снять: `remove` или `prune`.
- `remove` — teardown удаляет всё, что поставила задача.

Кто главнее, сверху вниз: флаг `teardown --keep|--remove`, `$AUTOHARNESS_TEARDOWN`,
`.autoharness/config.json` проекта, `~/.autoharness/config.json`, значение по умолчанию.

```bash
autoharness config set teardown remove            # для этого проекта
autoharness config set teardown remove --global   # для всех проектов
autoharness config get                            # текущее значение и откуда оно
```

`.autoharness/.gitignore` пропускает `config.json` и `kept.json`, так что их можно
закоммитить вместе с папкой skills; остальное состояние задач в git не попадает.

## Свои skills

В конце задачи агент может записать выученное (как собрать и проверить такой проект,
особенности API) в skill и сохранить его в библиотеку `~/.autoharness/skills/`:

```bash
autoharness skill new pptx-qa --description "Check .pptx slides visually"   # заготовка в папке skills
autoharness skill save pptx-qa              # проверки; код 3 — нужно согласие пользователя
autoharness skill save pptx-qa --approve    # в библиотеку
autoharness add skill local:pptx-qa         # в другом проекте
```

`skill save` блокирует секреты (токены, приватные ключи, `password=…`) и опасные паттерны,
предупреждает об абсолютных путях и email и без `--approve` ничего не сохраняет. Recipe
ссылается на такой skill как `local:<name>`; `need` показывает skills из библиотеки
с подходящей capability.

## Безопасность

- **Курированный каталог** (`src/autoharness/data/catalog.json`) — доверенная база.
- **MCP из registry** получают score: проверенный namespace, pinned-версия пакета,
  publisher из trusted-списка. Всё, что не «high», требует секреты или удалённый
  сервер, ставится только с `--approve` после явного «да» от пользователя.
- **Skills** сначала скачиваются во временную staging-папку, статически сканируются
  (чтение `~/.ssh`, `curl | sh`, «не говори пользователю», обфускация) и только
  потом копируются в проект. High-находки блокируют установку.
- **Удаляется только то, что записано в lockfile задачи или в `kept.json`.** Ваши
  собственные MCP и skills не перезаписываются и не удаляются; изменённый после
  установки skill сохраняется в бэкап (кэши вроде `__pycache__` правкой не считаются).
- Секреты не записываются: в конфиге только `${ENV_VAR}`.

Состояние задачи: `.autoharness/tasks/<id>/` (`task.json`, `mcp.lock.json`,
`skills.lock.json`, `permissions.json`, `harness.md`). Recipes:
`~/.autoharness/recipes/` (`AUTOHARNESS_HOME` переопределяет).

## Ограничения MVP

- Новые MCP-серверы агенты подхватывают только при старте сессии (Claude Code ещё через `/mcp`),
  так что hot-load в середине задачи требует перезапуска. Следующий шаг — gateway-MCP,
  который проксирует динамически добавленные серверы без рестарта.
- Классификация задачи — подсказка по ключевым словам: сравнение идёт по словам с учётом русских
  и английских окончаний (`text.py`), без стеммера и без LLM. Если подсказок нет, `init` отдаёт
  список всех capabilities, и выбирает агент.

## Тесты

```bash
pip install -e .[dev] && pytest
```
