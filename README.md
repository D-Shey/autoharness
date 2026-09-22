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

Какие capabilities нужны, решает агент, а не autoharness. `init` отдаёт подсказки
(`suggested`) — совпадения по ключевым словам с учётом русских и английских окончаний
(`text.py`, без стеммера и без LLM) — и всегда полный список `all_capabilities` каталога,
чтобы случайное совпадение не скрыло остальное.

## Установка

Установка состоит из трёх шагов: поставить CLI (один раз на машину), положить skill агенту
(в проект или глобально) и, по желанию, подключить gateway, чтобы новые MCP-серверы работали
без перезапуска агента.

### 1. CLI `autoharness`

Ставится через [uv](https://docs.astral.sh/uv/) как отдельный инструмент: uv сам берёт
подходящий Python (3.10+) и кладёт команду `autoharness` в свою папку для исполняемых файлов.
Внешних зависимостей нет.

Если uv ещё не стоит:

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Установка autoharness прямо из GitHub:

```bash
uv tool install git+https://github.com/D-Shey/autoharness
autoharness agents        # проверка: печатает поддерживаемых агентов и обнаруженного
```

Репозиторий приватный: uv берёт доступ из настроек git, поэтому сначала войдите, например,
через `gh auth login` (он настраивает git на ваш аккаунт).

Команда `autoharness` должна находиться через `PATH`: её вызывают и агент, и gateway. uv кладёт
её в `~/.local/bin` (на Windows — `%USERPROFILE%\.local\bin`; точный путь — `uv tool dir --bin`).
Если `autoharness agents` отвечает «command not found», выполните `uv tool update-shell` и
откройте новый терминал. Агента после этого тоже запускайте из нового терминала, иначе он не
увидит обновлённый `PATH`.

Обновление и удаление:

```bash
uv tool upgrade autoharness       # подтянуть последний коммит из GitHub
uv tool uninstall autoharness
```

Если вы правите сам autoharness, ставьте из локальной копии в режиме редактирования — изменения
в коде сразу видны команде `autoharness`:

```bash
git clone https://github.com/D-Shey/autoharness.git
cd autoharness
uv tool install -e .
```

Для большинства skills и MCP-серверов из каталога нужен ещё Node.js (`npx`); Python-серверы
запускаются через `uvx`, который ставится вместе с uv.

### 2. Skill для агента

Skill — это `SKILL.md`, из которого агент узнаёт, как пользоваться autoharness. В каждый агент
кладётся свой вариант: с правильным именем агента, путём к MCP-конфигу и подсказкой о перезапуске.
Поэтому **агент надо указывать явно**: в обычном терминале autoharness не знает, для кого
ставит, и по умолчанию берёт `claude`.

`--agent` — глобальный флаг, он пишется **до** подкоманды:

```bash
cd /path/to/your/project                       # команды работают с текущей папкой (или --project)

autoharness --agent claude   install-skill     # в проект: .claude/skills/autoharness/SKILL.md
autoharness --agent codex    install-skill --user   # глобально: ~/.agents/skills/autoharness/SKILL.md
autoharness install-skill --all                # в проект, сразу для всех пяти агентов
```

| Агент | `--agent` | Skill в проекте | Skill глобально (`--user`) |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/autoharness/` | `~/.claude/skills/autoharness/` |
| Codex CLI | `codex` | `.agents/skills/autoharness/` | `~/.agents/skills/autoharness/` |
| Gemini CLI | `gemini` | `.gemini/skills/autoharness/` | `~/.gemini/skills/autoharness/` |
| Qwen Code | `qwen` | `.qwen/skills/autoharness/` | `~/.qwen/skills/autoharness/` |
| GigaCode CLI | `gigacode` | `.gigacode/skills/autoharness/` | `~/.gigacode/skills/autoharness/` |
| Strands harness | `strands` | `.agent/skills/autoharness/` | `~/.agent/skills/autoharness/` (сам strands его не читает: передайте путь в `create_harness(skills=...)`) |

В проект или глобально:

- **в проект** — skill работает только в этом проекте, его можно закоммитить, и он будет
  у всей команды;
- **глобально (`--user`)** — skill есть в любом проекте на этой машине.

Агенты читают skills при старте сессии: после установки начните новую сессию.
Повторный `install-skill` перезаписывает `SKILL.md` — так он обновляется после
`git pull` в autoharness.

### 3. Gateway (по желанию, но рекомендуется)

Без gateway каждый новый MCP-сервер пишется прямо в конфиг агента, и агента нужно
перезапускать. С gateway перезапуск нужен **один раз** — после установки самого gateway:

```bash
autoharness --agent claude gateway install     # пишет сервер `autoharness` в конфиг агента
autoharness --agent claude gateway status      # установлен ли, какие серверы запущены
```

Gateway ставится в проект и **отдельно для каждого агента**: у `gateway install` нет `--all`,
повторите команду с нужным `--agent`. `add mcp` идёт через gateway только для того агента,
у которого gateway установлен.

Что сделать после `gateway install` в каждом агенте:

| Агент | Куда пишется gateway | Как агенту его подхватить |
|---|---|---|
| Claude Code | `.mcp.json` | перезапустить Claude Code (или `/mcp`) и **одобрить** проектный сервер `autoharness` |
| Codex CLI | `.codex/config.toml` | перезапустить Codex; проектный конфиг читается, только если проект помечен **trusted** |
| Gemini CLI | `.gemini/settings.json` | перезапустить или выполнить `/mcp refresh` |
| Qwen Code | `.qwen/settings.json` | перезапустить или выполнить `/mcp refresh` |
| GigaCode CLI | `.gigacode/settings.json` | перезапустить или выполнить `/mcp refresh` |
| Strands harness | `.agent/mcp.json` | передать `create_harness(mcp_servers=".agent/mcp.json")` и пересоздать агента |

Проверка: в агенте должны появиться инструменты `harness_tools` и `harness_call`. В Claude Code
они могут прийти отложенными — их находит поиск инструментов.

Если autoharness не найден в `PATH` в момент `gateway install`, в конфиг пишется
`python -m autoharness gateway serve` с полным путём к Python, в котором он запущен. Это работает,
пока этот Python на месте: после `uv tool uninstall` или переустановки выполните
`gateway install` заново, уже с `autoharness` в `PATH`.

### Проверка целиком

Попросите агента в новой сессии: «запусти autoharness init для задачи „прочитай
https://example.com“ и добавь fetch». Ожидаемо: агент вызывает `autoharness init`, затем
`autoharness add mcp fetch`; с gateway ответ содержит `"via": "gateway"`, и страница читается
через `harness_call` без перезапуска.

### Несколько агентов в одном проекте

```bash
autoharness install-skill --all
autoharness --agent claude gateway install
autoharness --agent codex  gateway install
```

Задачи и gateway у агентов общие (`.autoharness/`), а конфиги у каждого свои. Внутри сессии
агент определяется сам — по переменным окружения, которые он ставит своим командам
(`CLAUDECODE`, `CODEX_THREAD_ID`, `GEMINI_CLI`, …). Если определение не сработало, задайте
`AUTOHARNESS_AGENT=<агент>` в окружении, из которого запускаете агента.

### Удаление

```bash
autoharness --agent claude gateway uninstall   # убирает `autoharness` из конфига агента
```

Skill удаляется вместе с папкой `autoharness` в каталоге skills (таблица выше). CLI — `uv tool uninstall autoharness`.

## Агенты

| `--agent` | MCP-конфиг проекта | Skills проекта |
|---|---|---|
| `claude` (Claude Code) | `.mcp.json` | `.claude/skills` |
| `codex` (Codex CLI) | `.codex/config.toml` | `.agents/skills` |
| `gemini` (Gemini CLI) | `.gemini/settings.json` | `.gemini/skills` |
| `qwen` (Qwen Code) | `.qwen/settings.json` | `.qwen/skills` |
| `gigacode` (GigaCode CLI) | `.gigacode/settings.json` | `.gigacode/skills` |
| `strands` ([Strands harness](https://github.com/strands-agents/harness-sdk)) | `.agent/mcp.json` | `.agent/skills` |

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
| `inspect mcp\|skill <name>` | подробный trust-отчёт; для MCP ещё `egress` — куда сервер может ходить |
| `vet mcp\|skill <name> [--verdict fits\|breaks --text ...]` | промпт для независимой проверки кандидата против `--constraints` из brief; запись вердикта |
| `add mcp <name>` / `add skill owner/repo@skill` | ставит в MCP-конфиг и папку skills агента и пишет в lockfile |
| `remove mcp\|skill <name>` | снимает то, что поставила эта задача или сохранила в проекте прошлая |
| `prune` | снимает всё, что прошлые задачи сохранили в проекте |
| `status` | текущий harness, что сохранено в проекте, журнал |
| `teardown [--keep\|--remove] [--save-recipe NAME --notes ... --query kind:q]` | завершает задачу по политике `teardown`; recipe хранит и запросы к реестрам (`skill:<q>`/`mcp:<q>`), которые нашли поставленное |
| `config get\|set [teardown keep\|remove \| allowed_hosts "<хосты>"] [--global]` | настройки |
| `skill new\|save\|list\|delete` | свои skills агента и локальная библиотека |
| `gateway install\|uninstall\|status\|serve` | один MCP-сервер autoharness, через который новые серверы работают без перезапуска |
| `add mcp <name> --direct` | мимо gateway, сразу в конфиг агента |
| `recipe list\|match\|show` | сохранённые наборы; `show` перезапускает запросы recipe и отдаёт в `fresh` новых кандидатов, `init` делает то же для recipe со score ≥ 0.3 |
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

## Куда могут ходить MCP-серверы

Настройка `allowed_hosts` — список шаблонов хостов через запятую (`api.github.com,*.example.com`);
пусто (по умолчанию) — без ограничений. Честно о том, что она может:

- **удалённый** сервер (HTTP/SSE) живёт на одном известном хосте: хост не из списка — `add` просит
  согласия пользователя (код `3`), а gateway не подключается к нему и отключает уже работающий, если список сузили позже (в течение секунды);
- **локальный процесс** (`npx`, `uvx`, `docker`) ходит куда хочет, autoharness это не ограничивает.
  Поэтому при заданном списке такой сервер тоже ставится только с согласия пользователя.

```bash
autoharness config set allowed_hosts "api.githubcopilot.com,*.context7.com"
autoharness inspect mcp github      # поле egress: host, enforced, ok, reason
```

## Проверка кандидатов против ограничений

Если в brief записаны `--constraints` пользователя, `add` не ставит skill или сервер без вердикта
независимой проверки (статус `vet_required`). `autoharness vet skill <spec>` ставит skill во
временную папку и собирает промпт из ограничений дословно и содержимого файлов (для MCP — конфиг,
права и `egress`). Промпт отдаётся свежему subagent'у, его ответ записывается через
`--verdict fits|breaks --text "..."`. На `breaks` `add` отвечает кодом `3`: ставить только если
пользователь согласен. Если файлы skill при установке отличаются от проверенных, `add` предупреждает.
Свои skills из библиотеки (`local:`) проверку не требуют.

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

## Gateway: серверы без перезапуска

```bash
autoharness gateway install     # один раз; после этого — последний перезапуск агента
autoharness add mcp fetch       # доступен сразу: harness_tools / harness_call
autoharness gateway status      # что запущено и что упало
```

Gateway — MCP-сервер `autoharness` в конфиге агента. Он запускает серверы из
`.autoharness/gateway.json` (туда `add mcp` пишет, когда gateway установлен) и отдаёт агенту
два неизменных инструмента: `harness_tools` (серверы и их инструменты со схемами) и
`harness_call(server, tool, arguments)`. Они работают в любом агенте. Дополнительно gateway
отдаёт родные инструменты `<server>__<tool>` и шлёт `tools/list_changed`. В Claude Code это
проверено вживую (21.09.2026): после `add mcp fetch` без перезапуска появился
`mcp__autoharness__fetch__fetch` — отложенным инструментом, который подгружается через поиск
инструментов. В Codex и Gemini CLI родные инструменты не проверены, там переносимый путь —
мета-инструменты.

Через gateway идут локальные серверы и HTTP-серверы с заголовками (токен из env).
Серверы с OAuth-входом и `add mcp --direct` пишутся в конфиг агента, как раньше.
Секреты подставляются из окружения процесса агента; лог — `.autoharness/gateway.log`.
Локальный сервер получает окружение агента **без** переменных, похожих на секреты (`KEY`, `PASSWORD`,
`PASSWD`, `SECRET`, `TOKEN`, `CREDENTIAL` в имени): нужный секрет он видит, только если он указан в его `env`
как `${VAR}`. Серверы, записанные напрямую в конфиг агента (`--direct`), запускает сам агент — на них
это не распространяется.

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
- **Evidence для кода:** `autoharness evidence diff [--base REV]` сохраняет как result весь diff рабочей
  копии против `REV` (по умолчанию `HEAD`), включая новые неотслеживаемые файлы, без изменения индекса.

Состояние задачи: `.autoharness/tasks/<id>/` (`task.json`, `mcp.lock.json`,
`skills.lock.json`, `permissions.json`, `harness.md`). Recipes:
`~/.autoharness/recipes/` (`AUTOHARNESS_HOME` переопределяет).

## Ограничения MVP

- Gateway не умеет OAuth-вход и устаревший SSE-транспорт: такие серверы по-прежнему пишутся
  в конфиг агента и требуют перезапуска. Уведомления от HTTP-серверов gateway не слушает.
- Родные инструменты `<server>__<tool>` появляются только в агентах, которые обрабатывают
  `tools/list_changed`: в Claude Code — да, Codex и Gemini CLI не проверены. Мета-инструменты
  работают везде.

## Тесты

```bash
uv run --extra dev pytest      # uv сам создаёт .venv и ставит pytest
```
