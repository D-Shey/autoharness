# AutoHarness

**AutoHarness собирает агенту инструменты под конкретную задачу — и проверяет, что задача
действительно сделана.**

Coding-агент (Claude Code, Codex CLI, Gemini CLI, Qwen Code, GigaCode, Strands) обычно работает
с тем набором MCP-серверов и skills, который кто-то заранее прописал в конфиг. Этого набора либо
не хватает (нет браузера, чтобы посмотреть на страницу; нет актуальных доков API; нет референса,
под который надо попасть), либо он раздут: десятки серверов на все случаи жизни съедают контекст
и дают агенту доступ к тому, что задаче не нужно.

AutoHarness переворачивает это. Агент стартует почти «голым» — с одним skill `autoharness` — и
на каждую задачу сам строит себе *harness*: решает, какие возможности нужны, ищет подходящие
MCP-серверы и skills в публичных реестрах, проверяет их на доверие и на ограничения пользователя,
ставит, работает, проверяет результат руками независимого критика и в конце оставляет
поставленное в проекте или снимает. Удачные наборы сохраняются как *recipes* и переиспользуются
в похожих задачах.

## Как это работает

Для небольшой задачи с готовыми инструментами есть короткий режим:

```bash
autoharness init "исправить опечатку" --mode light
# работа и обычная проверка результата
autoharness teardown
```

`light` не запускает поиск и не требует brief. Если понадобилась новая возможность,
`autoharness mode full` включает поиск и полный цикл. `init --mode auto` выбирает
полный режим при совпадении задачи с capability из каталога; решение основано на
ключевых словах, поэтому агент может сразу указать `--mode full`. Без флага
сохраняется прежний полный цикл.

`--mode reviewed` — полный цикл плюс обязательный независимый критик перед
завершением. Идущую задачу можно поднять, не начиная заново: `mode full` или
`mode reviewed`. Старый флаг `--profile quick|standard|reviewed` по-прежнему
принимается как синоним `light|full|reviewed`.

`status` по умолчанию короткий: вместо сырых результатов поиска показывает по
строке на запрос и последние 30 строк лога. Всё целиком — `status --full`.

Для повторяемой проверки можно взять шаблон и сохранить доказательство:

```bash
autoharness verify guide api
autoharness brief --goal "..." --inputs "..." --constraints "..." \
  --verify-template api --candidates "..." --gaps "none: ..."
autoharness verify record api --passed yes --evidence api-test-output.txt
```

Есть шаблоны `browser`, `api`, `logs`, `database`. Они задают шаги и ожидаемое
доказательство, но сами не запускают приложение. `decision mcp|skill NAME
select|skip --reason "..."` фиксирует выбор кандидата; `audit` показывает
структурированный журнал решений, установок, проверок и удалений. Для установки
журнал хранит источник и отпечаток содержимого, а версию — когда она известна.

Работа, найденная по ходу задачи, но лежащая вне её (соседний баг, замечание критика
за пределами brief), не чинится сразу и не теряется:

```bash
autoharness followup add "проверка trust не учитывает X в registry.py"
autoharness followup list          # открытые; --all вместе с закрытыми
autoharness followup done 3 --note "исправлено в задаче про X"
```

Находки лежат в `.autoharness/followups.json`, который можно коммитить. `teardown`
перечисляет находки своей задачи, а следующий `init` в любом режиме показывает открытые.

Для сравнения пользы harness используйте одинаковый критерий успеха для двух
прогонов одной задачи:

```bash
autoharness eval record --case login-fix --variant baseline --passed no --seconds 420 --tokens 9000
autoharness eval record --case login-fix --variant harness --passed yes --task-id 20260925-120000-abcdef --tokens 7300
autoharness eval compare
```

Для завершённой задачи `--task-id` берёт время из `task.json` и число вызовов CLI
из `events.jsonl`. Токены, стоимость (`--cost`) и исход проверки вводятся по
данным среды агента; AutoHarness их самостоятельно не наблюдает. Отчёт считает
только задачи с обоими вариантами, показывает пропущенные пары и средние по
задачам. Эти числа показывают наблюдаемые различия, а не доказывают причину.

Для набора случаев подготовьте JSONL с уникальными `case` и полем `input` в каждой
строке. `eval batch` передаёт одну строку во входной поток внешнего runner и
ожидает один JSON-объект на выходе: `passed` (boolean), необязательные `tokens`,
`cost`, `tool_calls`, `task_id`. Время измеряется AutoHarness. Запускайте один и
тот же набор с обоими вариантами:

```bash
autoharness eval batch --dataset cases.jsonl --variant baseline -- python baseline_runner.py
autoharness eval batch --dataset cases.jsonl --variant harness -- python harness_runner.py
autoharness eval compare
```

Внешний runner выполняет задачу; AutoHarness не подменяет его оценку успеха и
не видит вызовы инструментов, если runner их не сообщил. Пакет сохраняется после
того, как runner успешно ответил на все случаи.

```
задача → init → capabilities → recipe? → поиск → brief → trust (+ vet) → add → smoke-test
                      ↑                                                            │
                      └────────── не хватает возможности (need) ◄──── работа ◄─────┘
                                                                         │
                                      проверка + критик → teardown (--save-recipe)
```

Две части делят работу так: **думает агент, считает и охраняет CLI**. Агент решает, что нужно
задаче и хорош ли результат. CLI `autoharness` ведёт учёт (lockfiles, что поставлено, кем и
зачем), ищет в реестрах, считает trust score, пишет конфиги агента, не даёт пропустить шаги и
всё отдаёт в JSON. Код выхода `3` всегда значит одно: *остановись и спроси пользователя*.

### 1. Разбор задачи — `init`

```bash
autoharness init "сверстай лендинг по макету из Figma"
```

`init` создаёт окружение задачи в `.autoharness/tasks/<id>/` и возвращает:

- `suggested` — подсказки capabilities по ключевым словам (с учётом русских и английских
  окончаний, без LLM) и всегда полный каталог `all_capabilities`, чтобы случайное совпадение
  не скрыло остальное. Решает всё равно агент;
- `recipes` — похожие сохранённые наборы;
- `discovered` — что уже нашлось под эту задачу на [skills.sh](https://skills.sh) и в
  [официальном MCP registry](https://registry.modelcontextprotocol.io);
- `rejected_before` — кандидаты из этого вывода, которых прошлые задачи отвергли (`decision skip`),
  сняли с причиной или признали нарушающими ограничения при vet, с причиной. Recipes помнят, что
  сработало, а это помнит, что нет. Если более поздняя задача кандидата выбрала или поставила, отказ
  больше не показывается. `add` такого кандидата предупреждает о том же.

Capability — это не «инструмент», а то, что нужно уметь: `browser-automation`, `library-docs`,
`visual-reference`, `visual-review`, `postgres`… Главный вопрос здесь не «чем сделать», а
**«чем проверить»**: веб-задаче нужен браузер, чтобы увидеть страницу, а не только редактор.
Отдельная и частая дыра — недостающий вход: агент рисует по памяти то, что можно найти и
посмотреть (кадр из фильма, макет, ответ API). Это тоже capability, и её закрывают первой.

### 2. Поиск — `need`, `search`

Агент читает всё, что нашёл `init`, и ищет дальше своими словами:

```bash
autoharness need library-docs --reason "нужны актуальные доки Next.js 16"
autoharness search mcp "figma"
autoharness search skill "landing page"
```

Порядок предпочтения: свой skill из локальной библиотеки → курированный каталог → высокий
trust из реестра → ничего. «Ничего» — законный ответ: если хватает shell, `gh`, `psql` или
`npx playwright`, сервер не нужен.

### 3. Brief — план до начала работы

```bash
autoharness brief --goal "..." --inputs "..." --constraints "..." --verify "..." \
  --candidates "figma-mcp: take, ...; some-skill: skip, ..." --gaps "..."
```

Brief фиксирует: какой результат считается хорошим и как это проверить; каких входных данных не
хватает и откуда они возьмутся; ограничения пользователя **дословно** («без новых зависимостей»,
«только офлайн»); вердикт по каждому найденному кандидату. Если нужного know-how нет ни в одном
skill, агент планирует написать свой (`--gaps "create <name>: ..."`) и пишет его до основной
работы. С установленным [хуком](docs/installation.md#4-хук-для-claude-code-по-желанию) Claude
Code не даёт агенту ничего менять, пока brief не записан.

### 4. Проверка и установка — `vet`, `inspect`, `add`

```bash
autoharness inspect mcp figma      # trust score, секреты, куда сервер может ходить
autoharness add mcp figma
autoharness add skill owner/repo@skill
```

Перед установкой каждый кандидат проходит несколько фильтров:

- **Trust score.** Курированный каталог доверенный; серверы из реестра оцениваются по
  проверенному namespace, закреплённой версии и издателю. Всё не «high», всё с секретами или
  удалённое ставится только после явного «да» пользователя (`exit 3` → `--approve`).
- **Статический скан skills.** Skill сначала скачивается во временную папку и проверяется на
  опасное: чтение `~/.ssh`, `curl | sh`, «не говори пользователю», обфускация. Такие — `rejected`.
- **Независимая проверка на ограничения.** Если в brief есть ограничения, `add` отвечает
  `vet_required`: `autoharness vet` собирает промпт с ограничениями и содержимым кандидата, его
  получает свежий субагент, и его ответ записывается без правок.
- **Сеть.** С настройкой `allowed_hosts` удалённый сервер вне списка и любой локальный процесс
  ставятся только с согласия пользователя.

`add` пишет сервер в конфиг нужного агента (или в gateway, см. ниже), skill — в его папку skills,
и всё — в lockfile задачи. Секреты в файлы не попадают, только `${ENV_VAR}`. Затем агент делает
один дешёвый вызов (smoke-test); не работает — `remove` и следующий кандидат.

### 5. Работа и новые пробелы

Агент решает задачу. Застрял — спрашивает себя: *поможет ли подумать ещё, или мне чего-то не
хватает?* Не видно то, что надо проверить (UI, сеть, базу, логи), угадывается API без доков,
рисуется то, чего нет перед глазами, — это пробел в возможностях, и агент возвращается к шагу 2
через `need`.

### 6. Проверка результата — evidence, критик, арбитр

AutoHarness не верит агенту на слово, что всё готово:

```bash
autoharness evidence add ref.png --kind reference   # с чем сравниваем
autoharness evidence add shot.png --kind result     # что получилось в этом раунде
autoharness evidence diff                           # для кода: весь diff рабочей копии
autoharness review --by self --diff "1 ...; 2 ..." --worst major
autoharness critic                                  # промпт для независимого критика
autoharness review --by critic --answer-file answer.txt
```

- Работа идёт раундами: результат → сравнение с референсом → исправление главного → снова.
- **Судит не автор.** `critic` выдаёт промпт для свежего субагента, который работу не делал.
  Его ответ сохраняется в файл как есть; autoharness сам читает из него список замечаний и
  `VERDICT`, и одноразовый `ANSWER-ID` не даёт подставить старый ответ. Критик обязателен, если
  этого требует capability задачи (например, `visual-review`) или brief записан с
  `--review critic`; тогда задача заканчивается, когда его вердикт по последнему результату —
  `minor` или `none`.
- **Защита от зацикливания.** Если одна и та же блокирующая проблема держится три раунда,
  `review` требует сменить подход (`approach`). Если не помогли несколько подходов — `arbiter`:
  независимый арбитр решает, чинить (с измеримой проверкой `check`) или признать предел.
- Для визуальных задач teardown требует референсы, минимум два раунда результата и финального
  критика. Последнее слово у пользователя: `teardown --accepted "<его слова>"`.

### 7. Конец задачи — teardown, skills, recipes

```bash
autoharness skill new pptx-qa --description "Check .pptx slides visually"
autoharness skill save pptx-qa                 # проверки; код 3 — нужно согласие пользователя
autoharness skill save pptx-qa --approve       # после «да» — в библиотеку
autoharness teardown --save-recipe landing-from-figma --notes "лендинг по макету"
```

- **Свои skills.** Всё, чему агент научился (как собрать и проверить такой проект, причуды API),
  он записывает в skill и сохраняет в библиотеку `~/.autoharness/skills/`: `skill save` проверяет
  его, блокирует секреты и опасные паттерны и сохраняет только с `--approve` после «да»
  пользователя. В другом проекте — `autoharness add skill local:pptx-qa`; recipe ссылается на
  такой skill как `local:<name>`, а `need` показывает его, если совпала capability.
- **Recipe** запоминает набор и поисковые запросы, которые его нашли. В похожей задаче `init`
  предложит recipe (score ≥ 0.3) и заново прогонит запросы: если появилось что-то лучше, оно
  окажется в `fresh`.
- **Что остаётся в проекте** — настройка `teardown`:
  - `keep` (по умолчанию) — поставленное остаётся, записи переходят в `.autoharness/kept.json`,
    следующие задачи видят его как `already_installed`. Снять: `remove` или `prune`.
  - `remove` — teardown удаляет всё, что поставила задача.

  Приоритет: флаг `teardown --keep|--remove` → `$AUTOHARNESS_TEARDOWN` → `.autoharness/config.json`
  проекта → `~/.autoharness/config.json` → по умолчанию.

  ```bash
  autoharness config set teardown remove            # для этого проекта
  autoharness config set teardown remove --global   # для всех проектов
  autoharness config get                            # текущее значение и откуда оно
  ```

## Пример

Пользователь просит Claude Code: *«нарисуй в SVG постер в стиле кадра из „Бегущего по лезвию“»*.

(Сессия условная; ответы CLI сокращены.)

```bash
autoharness init "SVG-постер в стиле кадра из Blade Runner, без генераторов"
# → suggested: visual-reference, visual-review; discovered: skills про SVG, браузерные MCP

autoharness brief --goal "узнаваемая композиция и палитра кадра, уровень стилизованного постера" \
  --inputs "сам кадр: найти веб-поиском" --constraints "без генераторов" \
  --verify "рендер SVG в PNG, сравнение рядом с кадром" \
  --candidates "svg-generator-skill: skip, это генератор; playwright: take, рендер" --gaps "none: ..."

autoharness add mcp playwright
# → {"status": "vet_required", ...}   в brief есть ограничения — сначала независимая проверка
autoharness vet mcp playwright                               # промпт → свежий субагент
autoharness vet mcp playwright --answer-file vet.txt         # его ответ: VERDICT: fits
autoharness add mcp playwright                               # → {"status": "ok", "added": "playwright", "via": "gateway"}

autoharness evidence add still.jpg --kind reference
autoharness evidence add poster-1.png --kind result
autoharness critic                                           # → субагент: «небо светлое, нет вертикалей», VERDICT: major
autoharness review --by critic --answer-file critic-1.txt
autoharness evidence add poster-2.png --kind result
autoharness critic                                           # → VERDICT: minor
autoharness review --by critic --answer-file critic-2.txt

autoharness teardown --save-recipe svg-poster --notes "стилизованный постер по кадру"
```

В следующий раз `init` для похожей задачи сразу предложит recipe `svg-poster`.

## Gateway: серверы без перезапуска

Обычно новый MCP-сервер агент видит только после перезапуска. Gateway убирает это: в конфиг
агента один раз прописывается MCP-сервер `autoharness`, и дальше `add mcp` пишет новые серверы в
`.autoharness/gateway.json`, а gateway запускает их на лету. Агенту он отдаёт два неизменных
инструмента — `harness_tools` (серверы, их инструменты и схемы) и
`harness_call(server, tool, arguments)`, — которые работают в любом агенте. Кроме того, gateway
публикует родные инструменты `<server>__<tool>` и шлёт `tools/list_changed`: в Claude Code они
появляются без перезапуска (проверено вживую 21.09.2026: после `add mcp fetch` появился
`mcp__autoharness__fetch__fetch` — отложенным инструментом, который находится поиском инструментов); в Codex и Gemini CLI это не проверено, там надёжный путь — мета-инструменты.

Через gateway идут локальные серверы и HTTP-серверы с заголовками. Серверы с OAuth-входом и
`add mcp --direct` пишутся прямо в конфиг агента. Серверам gateway передаёт только безопасный
минимум окружения, а сервер, пришедший в `gateway.json` через `git pull`, не запустится, пока
его не одобрят на этой машине (`autoharness gateway approve`).

В Codex проверены загрузка skill, вызовы через мета-инструменты и добавление/удаление
серверов в работающем потоке (`tests/test_codex_live.py`). Если новому серверу нужна
переменная окружения, которую gateway ещё не получает, CLI добавляет её имя в конфиг
и сообщает о необходимости перезапуска Codex.

## Безопасность коротко

- Ставится только то, что прошло trust-проверку, или то, на что пользователь сказал «да».
- Skills сканируются до копирования в проект; секреты в конфиги не пишутся.
- Ограничения пользователя проверяет независимый субагент, а не тот, кто хочет поставить.
- Удаляется только то, что поставил autoharness на этой машине; ваши серверы и skills не трогаются.
- Хук и `--approve` — рамка процедуры, а не песочница: честность агента держится на инструкции
  skill. Где граница каждой защиты — в [docs/security.md](docs/security.md).

## Быстрый старт

```bash
uv tool install git+https://github.com/D-Shey/autoharness   # CLI (нужен uv; для skills и MCP — Node.js)
autoharness agents                                          # проверка: агенты и обнаруженный
cd /path/to/your/project
autoharness --agent claude install-skill                    # skill для агента
autoharness --agent claude gateway install                  # по желанию: MCP без перезапусков
autoharness install-hook                                    # по желанию, Claude Code: обязательный brief
```

Репозиторий приватный: до `uv tool install` войдите в GitHub (`gh auth login`). Если после
установки `autoharness` не находится, выполните `uv tool update-shell` и откройте новый терминал.
Перезапустите агента и дайте ему задачу. Все агенты, пути, глобальная установка, обновление и
удаление — в [docs/installation.md](docs/installation.md).

| Агент | `--agent` |
|---|---|
| Claude Code | `claude` |
| Codex CLI | `codex` |
| Gemini CLI | `gemini` |
| Qwen Code | `qwen` |
| GigaCode CLI | `gigacode` |
| [Strands harness](https://github.com/strands-agents/harness-sdk) | `strands` |

## Документация

- [docs/installation.md](docs/installation.md) — установка для всех агентов, gateway, хук, удаление.
- [docs/reference.md](docs/reference.md) — все команды и где что лежит.
- [docs/security.md](docs/security.md) — как устроена каждая защита и где её граница.

## Ограничения

- Gateway не умеет OAuth-вход и устаревший SSE-транспорт: такие серверы пишутся в конфиг агента и
  требуют перезапуска. Уведомления от HTTP-серверов gateway не слушает.
- Родные инструменты `<server>__<tool>` появляются только в агентах, которые обрабатывают
  `tools/list_changed`: в Claude Code — да, Codex и Gemini CLI не проверены. Мета-инструменты
  работают везде.
- `allowed_hosts` ограничивает только удалённые серверы; локальный процесс ходит куда хочет.

## Разработка

```bash
git clone https://github.com/D-Shey/autoharness.git && cd autoharness
uv tool install -e .            # команда autoharness сразу видит правки
uv run --extra dev pytest       # тесты; uv сам создаёт .venv и ставит pytest
```
