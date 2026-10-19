# Справочник

Как всё это складывается в цикл — в [README](../README.md#как-это-работает).

Все команды печатают JSON. Код выхода `3` значит: нужно согласие пользователя. `--agent` и
`--project` — глобальные флаги, пишутся до подкоманды.

## Команды

| Команда | Что делает |
|---|---|
| `init "<задача>" [--mode light\|full\|reviewed\|auto]` | создаёт задачу; `full` по умолчанию, `light` использует готовые инструменты (вывод без каталога и поиска), `reviewed` — полный цикл с обязательным критиком, `auto` выбирает light/full по каталогу; `--profile quick\|standard\|reviewed` — старый синоним; открытые follow-ups в любом режиме, в полном ещё `rejected_before` — кандидаты, которых прошлые задачи отвергли, с причиной |
| `mode full\|reviewed` | поднимает идущую задачу: лёгкую в полный режим с поиском кандидатов, или любую до обязательного критика |
| `plan "<задача>"` | то же без побочных эффектов |
| `need <capability> --reason ...` | фиксирует capability gap, отдаёт курированных кандидатов и свои skills |
| `search mcp\|skill <query>` | официальный MCP registry / skills.sh, с trust score |
| `inspect mcp\|skill <name>` | подробный trust-отчёт; для MCP ещё `egress` — куда сервер может ходить |
| `brief --goal ... --domains ... --method ... --constraints ... [--verify ... \| --verify-template browser\|api\|logs\|database] [--review critic]` | план задачи: цель, области знаний (`<область>: skill|mcp|create <имя> (searched: <запрос>)`, не меньше двух; запрос должен быть реально выполнен через `autoharness search`, взятое — `take` в candidates, создаваемое — в gaps; создаваемые скиллы заполняются до первого результата), метод (как такое делают хорошо — по найденным и прочитанным источникам с URL и запросами `searched:`, или `nothing usable; searched: ...`; критик сверяет результат с ним), входы, ограничения, проверка, вердикты по кандидатам |
| `verify guide browser\|api\|api-client\|logs\|database` | шаги и ожидаемые доказательства типовой проверки |
| `verify record KIND --passed yes\|no --evidence FILE` | сохраняет исход проверки и файл доказательства |
| `decision mcp\|skill NAME select\|skip --reason TEXT` / `audit [TASK_ID]` | причина выбора и структурированный журнал решений |
| `followup add "<что, где>"` / `followup list [--all]` / `followup done N [--note ...]` | находки вне задачи: сохраняются в проекте, их показывают `teardown` и следующий `init` |
| `vet mcp\|skill <name> [--answer-file F]` | промпт для независимой проверки кандидата против ограничений; запись ответа |
| `add mcp <name>` / `add skill owner/repo@skill` | ставит в MCP-конфиг (или gateway) и папку skills агента, пишет в lockfile |
| `add mcp <name> --direct` | мимо gateway, сразу в конфиг агента |
| `remove mcp\|skill <name> [--reason TEXT]` | снимает то, что поставила эта задача или сохранила в проекте прошлая; причина удаления текущей задачи попадает в audit |
| `prune` | снимает всё, что прошлые задачи сохранили в проекте |
| `evidence add <file> --kind reference\|facts\|result` / `evidence diff [--base REV]` | референс, факты из него (снятые инструментом; без них при референсе в требованиях первый результат не принимается) или результат раунда; для кода — весь diff. Первая версия — block-in основы, первый раунд критика судит только её; правки после первого результата без критика считаются. `--closeup` (`--part`) — крупный план или часть последнего целого результата (кроп картинки меньше целого распознаётся сам). Критик видит последнюю версию каждого вида результата (diff, рендер, отчёт) с её частями, а не все черновики; доказательства `verify record` идут рядом и работу не вытесняют |
| `review` / `critic` | раунд проверки; промпт для независимого критика. Критик оценивает и каждую проверку из `verify run` (раздел «Runs»): проверка, которая только ищет имя или строку в файле, перестаёт засчитываться за свой пункт брифа |
| `approach` / `arbiter` / `arbitrate` / `check` | смена подхода к части, которая не проходит review; арбитр и его проверка |
| `teardown [--keep\|--remove] [--save-recipe NAME --notes ... --query kind:q] [--accepted ...]` | завершает задачу; `--accepted`/`--force` — только словами пользователя, хук их отклоняет, пока пользователь не написал ничего с начала задачи |
| `claim [<id сессии>] --reason ...` | забрать задачу у сессии агента, которая её начала: другая сессия получает от хука сводку чужой задачи и остаётся заблокированной; только по решению пользователя или если та сессия ушла |
| `status [--full]` | текущий harness, что сохранено в проекте, журнал; по умолчанию сжато (поиск — строка на запрос, лог — последние 30 строк) |
| `eval record --case NAME --variant baseline\|harness --passed yes\|no [--seconds N] [--task-id ID] [--tokens N] [--cost N] [--tool-calls N]` | сохраняет измерение прогона; завершённая задача задаёт время и статистику CLI |
| `eval batch --dataset FILE --variant baseline\|harness -- RUNNER [ARGS]` | запускает внешний runner на каждом JSONL случае и записывает измерения |
| `eval compare` | сравнивает только случаи, у которых есть оба варианта прогона |
| `skill new\|save\|list\|delete` | свои skills агента и локальная библиотека |
| `recipe list\|match\|show` | сохранённые наборы; `show` перезапускает запросы recipe и отдаёт новое в `fresh` |
| `config get\|set [teardown keep\|remove \| allowed_hosts "<хосты>" \| min_mode any\|full\|reviewed] [--global]` | настройки |
| `gateway install\|uninstall\|status\|serve\|approve` | MCP-сервер autoharness, через который новые серверы работают без перезапуска |
| `install-skill` / `install-hook` / `waive` | установка skill и хука; пропуск autoharness в сессии по слову пользователя |
| `agents` | поддерживаемые агенты и их пути |

## Где что лежит

| Путь | Что там |
|---|---|
| `.autoharness/tasks/<id>/` | состояние задачи: `task.json`, lockfiles, `harness.md`, `events.jsonl`, `decisions.jsonl`, evidence, ответы проверяющих |
| `.autoharness/evals.jsonl` | измерения прогонов; локальный файл, игнорируется Git |
| `.autoharness/kept.json`, `config.json`, `gateway.json`, `followups.json` | что осталось в проекте, настройки проекта, серверы gateway, отложенные находки; их можно коммитить (`.autoharness/.gitignore` пропускает только их), остальное состояние в git не попадает |
| `.autoharness/gateway.log` | лог gateway |
| `~/.autoharness/` | `recipes/`, библиотека своих skills `skills/`, глобальный `config.json`, одобрения gateway (`AUTOHARNESS_HOME` переопределяет) |
| `src/autoharness/data/catalog.json` | курированный каталог capabilities, серверов и доверенных издателей |
| `src/autoharness/skill/SKILL.md` | инструкция для агента, которую ставит `install-skill` |
