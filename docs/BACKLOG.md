# AutoHarness Engineering Backlog & Architectural Roadmap

> **Источник:** Сформировано на базе архитектурных дайджестов агентного тулинга (`harness_news`) от 21.11.2026, 16.11.2026 и 04.11.2026.  
> **Ключевой инвариант AutoHarness:** Zero Runtime Dependencies (только чистая стандартная библиотека Python 3.11+).

---

## 🧭 Приоритетная матрица инициатив

| Инициатива | Компонент | Приоритет | Статус | Первоисточник / Вдохновение |
|---|---|---|---|---|
| **1. Изоляция параллельных субагентов через Git Worktree** | `src/autoharness/swarm.py` | 🔥 P0 (Срочный) | ✅ Внедрено | [opencode-agent-teams](https://github.com/mdsohail99/opencode-agent-teams) |
| **2. Intent Lease & Receipt-Gated Completion в Swarm DAG** | `src/autoharness/swarm.py` | 🔥 P0 (Архитектурный) | ✅ Внедрено | [Morrow](https://github.com/fahzan1296/morrow) |
| **3. Офлайн AST-гейткипер безопасности для навыков и MCP-конфигов** | `src/autoharness/vet.py` | P1 (Безопасность) | ✅ Внедрено | [Clawvet](https://github.com/MohibShaikh/clawvet) |
| **4. In-process AI Gateway Circuit Breaker & Fallback** | `src/autoharness/gateway.py` | P1 (Отказоустойчивость) | ✅ Внедрено | [SinapsAI](https://github.com/roedyrustam/sinapsai) |
| **5. Machine-Native Runtime Perception для UI/E2E** | `src/autoharness/stencil.py` | P1 (Верификация) | ✅ Внедрено | [Reticle](https://github.com/reticlehq/reticle) |
| **6. Peer Review Challenge & Cross-Validation субагентов** | `src/autoharness/reviewers.py` | P2 (Качество) | ✅ Внедрено | [Perplexity Multiagent](https://t.me/ai_machinelearning_big_data/11401) |
| **7. Стандартизированная телеметрия сессий и метрики DAG** | `src/autoharness/telemetry.py` | P2 (Наблюдаемость) | ✅ Внедрено | [Harnext Agent](https://github.com/harnext/harnext-agent) |
| **8. TDD-харнесс enforcement правил окружения** | `src/autoharness/sentinel.py` | P2 (Надёжность) | ✅ Внедрено | [Aval](https://github.com/alvaroofernaandez/aval) |
| **9. Навык мобильного комплаенса (117 правил реджектов)** | `.autoharness/skills/mobile-compliance` | P3 (Экосистема) | ❌ Исключено (out of scope) | [App Store Compliance](https://github.com/mjmirza/app-store-compliance) |
| **10. Динамическое маскирование секретов в консольных логах** | `src/autoharness/secrets.py` | P3 (Безопасность) | ✅ Внедрено | [keyshroud](https://github.com/dinhkarate/keyshroud) |

---

## 📌 Детализация задач

### 1. [SWARM-WORKTREE] Изоляция субагентов через нативные Git Worktree
* **Проблема:** При параллельном запуске независимых узлов Swarm DAG (например, `t2_coder` и `t3_coder` в одном ярусе) модификация файлов в общем рабочем дереве приводит к гонкам перезаписи и грязным диффам.
* **Решение:** 
  - Нативная интеграция `git worktree add --detach <path> HEAD` через `subprocess` (чистый stdlib).
  - Выполнение команд задач в изолированном контексте рабочей копии.
  - Детерминированное слияние разрешённых артефактов в основной репозиторий после верификации.
  - Автоматическая очистка через `git worktree remove --force`.
  - Грациозный fallback для окружений без Git.

### 2. [SWARM-LEASE] Intent Lease & Receipt-Gated Completion (Morrow)
* **Проблема:** 
  1. Агенты не объявляют захватываемые области изменений, что ведёт к конфликтам при параллелизации.
  2. Задачи объявляют себя выполненными просто по коду завершения `0`, без доказательной квитанции об успешности проверок и неизменности артефактов.
* **Решение:**
  - **Intent Lease:** Регистрация намерений на пути (`allowed_paths`) с TTL и блокировкой пересекающихся путей. Если путь заблокирован другим активным воркером, задача ожидает освобождения или отклоняется.
  - **Receipt-Gated Completion:** Узел графа не переходит в статус `completed` без криптографической/хеш-квитанции (`SwarmReceipt`), включающей SHA-256 хэши артефактов, вердикт барьеров и подпись эмитента (`barrier_sentinel`).

### 3. [VET-AST] Офлайн AST-гейткипер безопасности для навыков и MCP (Clawvet)
* **Проблема:** В `vet.py` сейчас используются простые строковые и regex-эвристики (`~/.ssh`, `curl | sh`), которые легко обходятся обфускацией.
* **Решение:** Полноценный синтаксический AST-сканер на стандартном модуле `ast`:
  - Обнаружение динамического вызова `eval()`, `exec()`, `__import__()`.
  - Проверка опасных вызовов `subprocess.Popen(..., shell=True)` и прямых обращений к `.env`, `.ssh`, `.git/config`.
  - Детектирование rug-pull изменений при обновлении локальных навыков.

### 4. [GATEWAY-CIRCUIT] In-process AI Gateway Circuit Breaker (SinapsAI)
* **Проблема:** Вызовы внешних LLM и удалённых MCP-серверов в шлюзе могут зависать или приводить к каскадному отказу при исчерпании лимитов (429) или падении эндпоинтов (500).
* **Решение:**
  - Реализация паттерна Circuit Breaker на базе `urllib.request` (состояния: `CLOSED`, `OPEN`, `HALF_OPEN`).
  - Экспоненциальный бэкофф и автоматический фолбэк на резервную модель/провайдера.
  - In-process семантическое кэширование повторных запросов.

### 5. [VERIFY-PERCEPTION] Machine-Native Runtime Perception (Reticle)
* **Проблема:** Верификация фронтенд-артефактов и UI ограничена юнит-тестами и логами консоли.
* **Решение:** 
  - Связка `stencil scan` с Playwright MCP для извлечения реального DOM-дерева и скриншотов рендера.
  - Машинная верификация расположения элементов и контраста без слепого гадания.
