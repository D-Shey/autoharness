# Генерация собственных skills — дизайн

Дата: 2026-09-21. Статус: на ревью.

## Зачем

Агент по ходу задачи узнаёт вещи, которых нет ни в одном поставленном skill: как собрать
именно этот проект, как проверить результат доступными средствами (например, выгрузить
слайды .pptx в PNG через PowerPoint COM, когда нет LibreOffice), какие у API особенности.
Сейчас это знание пропадает вместе с сессией.

Цель — **перенос опыта**: в конце задачи агент записывает выученное в skill, skill
попадает в библиотеку autoharness и ставится в других проектах вместе с recipe.

Пишет skill агент: у autoharness нет LLM. autoharness даёт заготовку, проверки,
учёт в lockfile, согласие пользователя и хранение.

Вне рамок: публикация на skills.sh, синхронизация библиотеки между машинами,
генерация skill посреди задачи вместо ненайденного (механизм тот же, но инструкции
агенту описывают только сценарий «в конце задачи»).

## Термины

- **Библиотека** — `~/.autoharness/skills/<name>/` (`AUTOHARNESS_HOME` переопределяет корень).
- **Сгенерированный skill** — skill, созданный `skill new` в текущей задаче.
- **Локальный spec** — `local:<name>`, ссылка на skill из библиотеки. Живёт рядом с
  `owner/repo@skill` в lockfile, `kept.json` и recipes.

## Команды

Все печатают JSON; код выхода `3` — нужно согласие пользователя.

| Команда | Нужна активная задача | Что делает |
|---|---|---|
| `skill new <name> --description "<текст>"` | да | заготовка в папке skills агента + запись в lockfile |
| `skill save <name> [--approve]` | нет | проверки; с `--approve` — копия в библиотеку |
| `skill list` | нет | содержимое библиотеки |
| `skill delete <name>` | нет | удаление из библиотеки |
| `add skill local:<name>` | да | установка из библиотеки |

### `skill new`

- Имя: `^[a-z0-9][a-z0-9-]{1,63}$`. Ошибка, если папка с таким именем уже есть в skills
  проекта или skill с таким именем есть в библиотеке (перезапись библиотеки — только через
  `skill save` уже установленного `local:` skill, см. ниже).
- Пишет `<skills_dir>/<name>/SKILL.md`:

  ```markdown
  ---
  name: <name>
  description: <описание>
  ---

  # <name>

  ## When to use
  <!-- autoharness: fill in -->

  ## Steps
  <!-- autoharness: fill in -->

  ## How to verify
  <!-- autoharness: fill in -->
  ```

- Запись в `skills.lock.json` задачи: `spec: "local:<name>"`, `source: "generated"`,
  `sha256` заготовки, `trust: {"level": "high", "score": 100}`, `findings: []`.
  Дальше этот skill для teardown, `keep`/`remove`, `status` ничем не отличается от других.

### `skill save`

Ищет skill по имени: сначала в lockfile активной задачи, затем в `kept.json` проекта.
Сохранять можно только skill со spec `local:<name>` (созданный `skill new` или
поставленный из библиотеки). Иначе ошибка: чужие skills в библиотеку не копируются.

Проверки по порядку; первая группа, давшая результат, определяет ответ:

1. **Формат** → `status: "invalid"`, код 1.
   Нет `SKILL.md`; нет frontmatter; `name` ≠ имя папки; пустой `description`;
   в любом файле остался маркер `<!-- autoharness: fill in -->`.
2. **Секреты** → `status: "blocked"`, код 1. Согласие не помогает, агент должен убрать.
   Паттерны: `ghp_[A-Za-z0-9]{20,}`, `github_pat_`, `sk-[A-Za-z0-9_-]{20,}`, `AKIA[0-9A-Z]{16}`,
   `xox[abpr]-`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`,
   `(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['"]?[^\s'"$<{]{8,}`
   (значения вида `${VAR}` и `<...>` не считаются).
3. **Статический скан** (`trust.scan_skill`) → high-находки: `status: "blocked"`, код 1.
4. **Приватность** → не блокирует, попадает в `warnings`:
   абсолютные пути `[A-Za-z]:\\Users\\[^\\\s]+`, `/home/[^/\s]+/`, `/Users/[^/\s]+/`;
   email-адреса. Medium/low-находки скана тоже идут в `warnings`.

Если блокирующего нет:

- без `--approve` → `status: "approval_required"`, код 3, в ответе `name`, `description`,
  `files`, `warnings`, `overwrites` (true, если skill с таким именем уже в библиотеке),
  `next`: показать пользователю и повторить с `--approve` только после его «да»;
- с `--approve` → копия (без `RUNTIME_JUNK`) в `~/.autoharness/skills/<name>/`, прежняя
  версия заменяется целиком. Рядом `~/.autoharness/skills/<name>.meta.json`:
  `{"name", "description", "capabilities", "task", "project", "saved", "sha256"}`,
  где `capabilities` — capabilities задачи на момент сохранения. В лог задачи (если она есть)
  пишется строка о сохранении.

`meta.json` лежит рядом с папкой, а не внутри, чтобы не попадать в сам skill при установке.

### `add skill local:<name>`

- Нет в библиотеке → ошибка с подсказкой `skill list`.
- Trust-отчёт: `score 100, level high`, причина «saved to the local library with the user's
  approval»; согласия не требует.
- Установка идёт тем же `Task.add_skill` с другим stager: он копирует папку из библиотеки
  вместо `npx skills`. Статический скан выполняется как обычно.
- Уже сохранённый в проекте (`kept.json`) → `already_installed`, как для любых skills.

### `skill list`, `skill delete`

- `list`: `[{name, description, capabilities, saved}]` из `*.meta.json`.
- `delete <name>`: удаляет папку и `meta.json`. Копии, уже поставленные в проекты, не трогает.
  Нет такого → ошибка.

## Связь с остальным

- **`parse_skill_spec`** принимает `local:<name>` и возвращает `("local", name)`.
- **Recipes.** `recipes.save` уже берёт specs из lockfile, `local:<name>` попадёт туда сам.
  `teardown --save-recipe` добавляет в ответ `warnings`, если в recipe есть `local:<name>`,
  которого нет в библиотеке.
- **Teardown.** Для skill с `source: "generated"`, которого нет в библиотеке:
  - `keep` — остаётся в проекте, в ответе `unsaved_generated: [name]`;
  - `remove` — перед удалением копия в `.autoharness/tasks/<id>/generated-skills/<name>/`,
    в ответе `unsaved_generated` с путём копии.
- **`need <capability>`** добавляет `local: [{name, description}]` — skills библиотеки,
  у которых эта capability есть в `meta.json`.
- **`SKILL.md` autoharness**, шаг 6, новый пункт перед teardown: если узнал переиспользуемое,
  чего нет в поставленных skills, — `skill new` → заполнить → `skill save` → показать
  пользователю → `--approve` после «да»; писать обобщённо, без абсолютных путей, секретов и
  имён людей и клиентов. Шаг 2: сначала `need`, он показывает и библиотеку.
- **README**: раздел «Свои skills», строка в таблице команд, убрать пункт из «Ограничений MVP».

## Размещение в коде

- `src/autoharness/library.py` — новый модуль: пути библиотеки, `save(src_dir, meta)`,
  `list_skills()`, `delete(name)`, `stage_from_library(repo, skill, stage_dir)` (stager),
  `check(skill_dir) -> CheckResult` (формат, секреты, скан, приватность).
- `env.py` — `Task.new_skill(name, description)`; `parse_skill_spec` для `local:`;
  в `teardown` — обработка несохранённых generated.
- `cli.py` — подкоманды `skill new|save|list|delete`, ветка `local:` в `resolve_skill`
  и выборе stager в `cmd_add`, `local` в `need`, предупреждения recipe в `teardown`.
- `trust.py` не меняется, проверки секретов и приватности живут в `library.py`: у них другая
  цель (утечка из проекта, а не угроза для агента).

## Ошибки

| Ситуация | Ответ |
|---|---|
| `skill new` без активной задачи | ошибка «no active task» (как у `add`) |
| имя невалидно или занято | ошибка с причиной |
| `skill save` для не-`local:` skill | ошибка «only skills created with `skill new` or installed from the library» |
| skill не найден ни в задаче, ни в `kept.json` | ошибка |
| повреждённый `meta.json` | `skill list` пропускает запись и добавляет её в `warnings` |

## Тесты

1. Полный цикл: `skill new` → заполнить → `skill save` (код 3, `approval_required`) →
   `skill save --approve` → в другом проекте `add skill local:<name>` ставит его, lockfile
   содержит `local:<name>`.
2. Незаполненная заготовка → `invalid`, код 1.
3. Секрет (`ghp_…`) → `blocked`, код 1, даже с `--approve`.
4. Абсолютный путь → `approval_required` с `warnings`.
5. Повторное сохранение → `overwrites: true`, после `--approve` версия в библиотеке новая.
6. Recipe с несохранённым `local:` → `warnings` в ответе teardown.
7. `teardown --remove` с несохранённым generated → копия в `generated-skills/`,
   `unsaved_generated` в ответе.
8. `need` показывает skill из библиотеки с подходящей capability.
9. `skill delete` удаляет из библиотеки, поставленная копия в проекте остаётся.
10. `skill save` для обычного `owner/repo@skill` → ошибка.
