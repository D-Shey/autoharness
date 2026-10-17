# Установка

Что такое autoharness и как он работает — в [README](../README.md). Здесь — только установка.

Установка состоит из трёх шагов: поставить CLI (один раз на машину), положить skill агенту
(в проект или глобально) и, по желанию, подключить gateway, чтобы новые MCP-серверы работали
без перезапуска агента. Для Claude Code есть ещё четвёртый, необязательный шаг — хук.

## 1. CLI `autoharness`

Ставится через [uv](https://docs.astral.sh/uv/) как отдельный инструмент: uv сам берёт
подходящий Python (3.10+) и кладёт команду `autoharness` в свою папку для исполняемых файлов.
На Python 3.11+ внешних зависимостей нет. На Python 3.10 автоматически ставится
`tomli` для корректного чтения конфигурации Codex.

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

Для большинства skills и MCP-серверов из каталога нужен ещё Node.js (`npx`, в том числе
`npx skills` для установки skills); Python-серверы запускаются через `uvx`, который ставится
вместе с uv.

## 2. Skill для агента

Skill — это `SKILL.md`, из которого агент узнаёт, как пользоваться autoharness. В каждый агент
кладётся свой вариант: с правильным именем агента, путём к MCP-конфигу и подсказкой о перезапуске.
Поэтому **агент надо указывать явно**: в обычном терминале autoharness не знает, для кого
ставит, и по умолчанию берёт `claude`.

`--agent` — глобальный флаг, он пишется **до** подкоманды:

```bash
cd /path/to/your/project                       # команды работают с текущей папкой (или --project)

autoharness --agent claude   install-skill     # в проект: .claude/skills/autoharness/SKILL.md
autoharness --agent codex    install-skill --user   # глобально: ~/.agents/skills/autoharness/SKILL.md
autoharness install-skill --all                # в проект, сразу для всех агентов
```

| Агент | `--agent` | Skill в проекте | Skill глобально (`--user`) |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/autoharness/` | `~/.claude/skills/autoharness/` |
| Codex CLI | `codex` | `.agents/skills/autoharness/` | `~/.agents/skills/autoharness/` |
| Gemini CLI | `gemini` | `.gemini/skills/autoharness/` | `~/.gemini/skills/autoharness/` |
| Qwen Code | `qwen` | `.qwen/skills/autoharness/` | `~/.qwen/skills/autoharness/` |
| GigaCode CLI | `gigacode` | `.gigacode/skills/autoharness/` | `~/.gigacode/skills/autoharness/` |
| Strands harness | `strands` | `.agent/skills/autoharness/` | `~/.agent/skills/autoharness/` (сам strands его не читает: передайте путь в `create_harness(skills=...)`) |
| Antigravity CLI | `agy` | `.agents/skills/autoharness/` | `~/.gemini/config/skills/autoharness/` |

Codex и Antigravity CLI читают один и тот же `.agents/skills`, поэтому `SKILL.md` в проекте у
них общий: в нём указаны `--agent` и MCP-конфиг для каждого из двух агентов. Глобальные
skills у них разные: `~/.agents/skills` agy не читает. В `/skills` agy 1.4.0 предлагает для
глобальных skills `~/.gemini/antigravity-cli/skills`, но загружает их только из
`~/.gemini/config/skills` — поэтому `--user` пишет туда.

В проект или глобально:

- **в проект** — skill работает только в этом проекте, его можно закоммитить, и он будет
  у всей команды;
- **глобально (`--user`)** — skill есть в любом проекте на этой машине.

Агенты читают skills при старте сессии: после установки начните новую сессию.
Повторный `install-skill` перезаписывает `SKILL.md` — так он обновляется после
обновления autoharness. `init` предупреждает, если установленный `SKILL.md` устарел.

## 3. Gateway (по желанию, но рекомендуется)

Без gateway каждый новый MCP-сервер пишется прямо в конфиг агента, и агента нужно
перезапускать. С gateway первый перезапуск нужен после установки самого gateway
(как он устроен — в [README](../README.md#gateway-серверы-без-перезапуска)):

```bash
autoharness --agent claude gateway install     # пишет сервер `autoharness` в конфиг агента
autoharness --agent claude gateway status      # установлен ли, какие серверы запущены
```

Gateway ставится в проект и **отдельно для каждого агента**: у `gateway install` нет `--all`,
повторите команду с нужным `--agent`. `add mcp` идёт через gateway только для того агента,
у которого gateway установлен.

Для Codex возможен дополнительный перезапуск, если новому серверу нужна ещё не
передаваемая gateway переменная окружения. `add` и `gateway approve` сообщают об этом
и записывают её имя в `env_vars`; значение нужно задать в окружении запуска Codex.

Что сделать после `gateway install` в каждом агенте:

| Агент | Куда пишется gateway | Как агенту его подхватить |
|---|---|---|
| Claude Code | `.mcp.json` | перезапустить Claude Code (или `/mcp`) и **одобрить** проектный сервер `autoharness` |
| Codex CLI | `.codex/config.toml` | перезапустить Codex; проектный конфиг читается, только если проект помечен **trusted** |
| Gemini CLI | `.gemini/settings.json` | перезапустить или выполнить `/mcp refresh` |
| Qwen Code | `.qwen/settings.json` | перезапустить или выполнить `/mcp refresh` |
| GigaCode CLI | `.gigacode/settings.json` | перезапустить или выполнить `/mcp refresh` |
| Strands harness | `.agent/mcp.json` | передать `create_harness(mcp_servers=".agent/mcp.json")` и пересоздать агента |
| Antigravity CLI | `.agents/mcp_config.json` | перезапустить `agy` |

Проверка: в агенте должны появиться инструменты `harness_tools` и `harness_call`. В Claude Code
они могут прийти отложенными — их находит поиск инструментов.

В конфиг записываются полный путь к текущему Python и команда
`-m autoharness --project <абсолютный путь проекта> gateway serve`. Это исключает
случайный запуск другой установки из `PATH` и выбор другого проекта по cwd. Это работает,
пока этот Python на месте: после `uv tool uninstall` или переустановки выполните
`gateway install` заново, уже с `autoharness` в `PATH`.

## 4. Хук для Claude Code (по желанию)

Без хука агент идёт через autoharness, когда сам решит, что skill подходит. Хук делает это
обязательным: на первый содержательный запрос сессии он напоминает агенту вызвать skill и до
записанного brief запрещает инструменты, которые что-то меняют (`Write`, `Edit`, `Bash`,
`PowerShell`, MCP). Чтение файлов остаётся открытым.

```bash
autoharness install-hook            # в .claude/settings.json проекта
autoharness install-hook --user     # в ~/.claude/settings.json, для всех проектов
autoharness install-hook --remove   # убрать
```

Действует в новых сессиях (или откройте `/hooks` в текущей). Пропустить autoharness в одной
сессии, если пользователь так сказал: `autoharness waive <session id> --reason "<его слова>"`.

## Проверка целиком

Попросите агента в новой сессии: «запусти autoharness init для задачи „прочитай
https://example.com“ и добавь fetch». Ожидаемо: агент вызывает `autoharness init`, затем
`autoharness add mcp fetch`; с gateway ответ содержит `"via": "gateway"`, и страница читается
через `harness_call` без перезапуска.

## Несколько агентов в одном проекте

```bash
autoharness install-skill --all
autoharness --agent claude gateway install
autoharness --agent codex  gateway install
```

Задачи и gateway у агентов общие (`.autoharness/`), а конфиги у каждого свои. Внутри сессии
агент определяется сам — по переменным окружения, которые он ставит своим командам
(`CLAUDECODE`, `CODEX_THREAD_ID`, `GEMINI_CLI`, `ANTIGRAVITY_AGENT`, …). `ANTIGRAVITY_AGENT`
проверяется первым: agy, запущенный из Claude Code, наследует его `CLAUDECODE`. Порядок выбора: `--agent`, затем
`$AUTOHARNESS_AGENT`, затем автоопределение, иначе `claude`; агент запоминается в задаче при
`init`. Если определение не сработало, задайте `AUTOHARNESS_AGENT=<агент>` в окружении, из
которого запускаете агента.

## Как autoharness пишет в конфиги разных агентов

| `--agent` | MCP-конфиг проекта | Skills проекта |
|---|---|---|
| `claude` (Claude Code) | `.mcp.json` | `.claude/skills` |
| `codex` (Codex CLI) | `.codex/config.toml` | `.agents/skills` |
| `gemini` (Gemini CLI) | `.gemini/settings.json` | `.gemini/skills` |
| `qwen` (Qwen Code) | `.qwen/settings.json` | `.qwen/skills` |
| `gigacode` (GigaCode CLI) | `.gigacode/settings.json` | `.gigacode/skills` |
| `strands` ([Strands harness](https://github.com/strands-agents/harness-sdk)) | `.agent/mcp.json` | `.agent/skills` |
| `agy` (Antigravity CLI) | `.agents/mcp_config.json` | `.agents/skills` |

Lockfiles и recipes хранят конфиг в одном каноническом виде (формат `.mcp.json`,
секреты как `${VAR}`), адаптер переводит его при записи: для Gemini-семейства
remote-серверы становятся `httpUrl`/`url`, для Codex секреты уходят в `env_vars`,
`bearer_token_env_var`, `env_http_headers`. Codex не раскрывает `${VAR}` в `args`;
в этом случае `add` возвращает `warnings`. Antigravity CLI тоже не раскрывает `${VAR}`, но
передаёт серверам своё окружение: `"K": "${K}"` просто не записывается, remote-серверы
становятся `serverUrl`, а для `${VAR}` в заголовках, `args` и переименованных переменных
`add` возвращает `warnings`. Проектный `.codex/config.toml` Codex читает
только для trusted-проектов. Правки в TOML обёрнуты маркерами `# >>> autoharness:<name>`,
так что комментарии и остальной конфиг пользователя не трогаются.

## Удаление

```bash
autoharness --agent claude gateway uninstall   # убирает `autoharness` из конфига агента
autoharness install-hook --remove              # убирает хук
autoharness prune                              # снимает всё, что задачи оставили в проекте
```

Skill удаляется вместе с папкой `autoharness` в каталоге skills (таблица выше). CLI — `uv tool uninstall autoharness`.
