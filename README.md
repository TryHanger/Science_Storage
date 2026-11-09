# Research Engine MVP v0.1

Автономный движок систематического скрининга научной литературы (**OpenAlex → PostgreSQL → Review Packages (50 публикаций) → AI / ChatGPT Screening → Strict Validation → Transactional Commit**).

---

## 1. Архитектура и сквозной цикл

```
+------------------+         +--------------------+         +------------------------+
|  OpenAlex API    | ------> | PostgreSQL (16/17) | ------> | ZIP Review Package     |
| (or fixture.json)|         | 6 таблиц, UUID PK  |         | (50 публикаций)        |
+------------------+         +--------------------+         +------------------------+
                                                                        |
                                                                        v
+------------------+         +--------------------+         +------------------------+
| Следующая партия | <------ | results.json       | <------ | Ручной шаг в ChatGPT   |
| (next 50 papers) |         | Строгая валидация  |         | (Screening Title/Abs)  |
+------------------+         +--------------------+         +------------------------+
```

### Шесть таблиц БД (SQL Schema):
1. `works` — уникальные публикации (нормализованный DOI, canonical OpenAlex ID, заголовок, аннотация, год, первый автор).
2. `search_runs` — provenance поисковых запросов OpenAlex (курсоры пагинации, статистика найдено/новых/дублей, ошибки).
3. `source_records` — исходный JSONB сырых записей провайдера с привязкой к `work_id` и `search_run_id`.
4. `review_jobs` — задания на скрининг с хешем протокола (SHA-256), состоянием `exported|repair_required|committed|cancelled` и путем к пакету.
5. `review_job_items` — точный снапшот и порядковый номер (1..N) публикаций, выданных в задание.
6. `paper_reviews` — закоммиченные экспертные решения (`include|exclude|uncertain`, `evidence_level`, `needs_fulltext`, `reason`).

---

## 2. Быстрый старт на Windows (PowerShell)

### Требования:
- **Python 3.11+**
- **Docker Desktop** (Docker Compose)
- **Windows PowerShell** или **PowerShell 7 (pwsh)**

### Шаг 1. Запуск PostgreSQL

В репозитории подготовлен `compose.yaml`, поднимающий PostgreSQL 16 с привязкой **строго к `127.0.0.1:5434`** (без открытия во внешнюю сеть):

```powershell
docker compose up -d
```

Проверка статуса контейнера:
```powershell
docker compose ps
```

### Шаг 2. Настройка окружения и установка

Создайте `.env` на основе примера:
```powershell
Copy-Item .env.example .env
```

Установите проект и зависимости:
```powershell
python -m pip install -e .
```

### Шаг 3. Инициализация базы данных

Команда идемпотентно применяет `contracts/schema.sql` и проверяет наличие ровно 6 таблиц:
```powershell
research db init
```

---

## 3. Сквозной демонстрационный сценарий (E2E)

### 3.1. Импорт публикаций

#### Вариант А: Офлайн-фикстура (детерминированная, без сети и лимитов)
```powershell
research import-openalex --query "HVAC FDD fixture" --fixture contracts/papers.jsonl
```

#### Вариант Б: Реальный OpenAlex API
```powershell
research import-openalex --query "fault detection HVAC" --limit 500 --year-from 2020
```
> **Примечание об OpenAlex Rate Limits:** 
> При отсутствии персонального ключа (`OPENALEX_API_KEY`) в `.env` запросы к публичному пулу OpenAlex могут встречать временное ограничение `HTTP 429`. Движок автоматически читает заголовок `Retry-After` / `retryAfter` в теле ответа, логирует время ожидания и выполняет повторные попытки с экспоненциальным backoff. Для непрерывной работы без задержек укажите бесплатный ключ в `.env`.

### 3.2. Экспорт пакета на скрининг (50 публикаций)

```powershell
research next --study buildingpulse_fdd --protocol contracts/research_protocol.md --count 50
```

Команда:
1. Бронирует следующие 50 публикаций в одной DB-транзакции.
2. Формирует ZIP-архив в `exchange/outgoing/<job_id>.zip`:
   - `manifest.json` — метаданные, список `work_ids`, SHA-256 протокола и хеш `papers.jsonl`.
   - `papers.jsonl` — публикации с полями `work_id`, `title`, `doi`, `publication_year`, `abstract`, `openalex_id`.
   - `research_protocol.md` — зафиксированный протокол исследования.
   - `instructions.md` — системная инструкция.
   - `response_schema.json` — строгая JSON Schema ответа.
3. Выводит путь к ZIP и готовый текст промпта для передачи в ChatGPT.

> **Гарантия уникальности задания:** Если у вас уже есть незавершённое задание (со статусом `exported` или `repair_required`), повторный вызов `research next` вернёт путь к **существующему** пакету, предотвращая распыление выборки и утечку ID.

### 3.3. Ручной шаг исследователя в ChatGPT

1. Пользователь открывает интерфейс ChatGPT (GPT-4o / Claude 3.5 Sonnet / etc.).
2. Прикрепляет скачанный файл `exchange/outgoing/<job_id>.zip` (или текст `papers.jsonl`).
3. Вставляет готовый промпт, выведенный командой `research next`:
   ```text
   Выполни screening статей по приложенному заданию Research Engine (Job ID: <job_id>).
   1. Открой ZIP-пакет (файлы manifest.json, papers.jsonl, research_protocol.md, instructions.md, response_schema.json).
   2. Оцени строго каждую из 50 статей по research_protocol.md, используя исключительно title и abstract. Не придумывай facts/fulltext.
   3. Обязательно обработай ровно все 50 идентификаторов work_id без пропусков, изменений и дубликатов.
   4. Сформируй и верни один скачиваемый файл results.json, строго валидный по response_schema.json.
   ```
4. Скачивает полученный файл `results.json` и помещает в `exchange/incoming/results.json`.

*(Для локального автоматического теста и демонстрации без ручного обращения к ChatGPT можно использовать синтетический контракт `contracts/example_results.json`).*

### 3.4. Приём и строгая валидация ответа

```powershell
research accept --file exchange/incoming/results.json
```

Валидатор проверяет:
- Корректность синтаксиса JSON (с указанием строки/колонки при ошибке).
- Соответствие `response_schema.json` (Draft 2020-12, запрет любых лишних полей `additionalProperties: false`).
- Совпадение `job_id`, `schema_version` и `protocol_sha256` с записью в БД.
- Точную кардинальность: все 50 `work_id` должны присутствовать ровно один раз (без дублей, пропусков и чужих ID).
- Статус задания (отменённые задания отклоняются).
- Идемпотентность: повторная отправка идентичного файла сообщает `already accepted` (безопасный no-op). Попытка передать другой файл для уже закоммиченного задания вызывает конфликт (перезапись запрещена).

**В случае успеха:**
- В единой DB-транзакции создаются ровно 50 записей в `paper_reviews`.
- Задание помечается как `committed`.
- Сырой ответ архивируется в `exchange/accepted/<job_id>/results.json`.

**В случае ошибки:**
- 0 записей в `paper_reviews` (транзакционный откат).
- Задание переводится в статус `repair_required`.
- Невалидный ответ сохраняется в `exchange/rejected/<job_id>_<timestamp>/`.
- Формируется детальный `validation_errors.json` и готовый `repair_prompt.txt` для повторной отправки в ChatGPT.

### 3.5. Просмотр статуса исследования

```powershell
research status --study buildingpulse_fdd --protocol contracts/research_protocol.md
```

Выводит сводную таблицу:
- Всего публикаций в базе
- Ожидают проверки (Pending)
- Закоммичено решений (Committed Reviews)
- Распределение заданий (Exported, Repair Required, Committed, Cancelled)
- Текущий активный Job (если есть)

### 3.6. Отмена зависшего задания

Если вы хотите аннулировать ошибочно выгруженный пакет и вернуть статьи в пул доступных для новых заданий:
```powershell
research cancel --job <UUID>
```

### 3.7. Оценка качества по эталонному набору (Gold Standard)

```powershell
research evaluate-gold --file contracts/gold_set_template.csv --study buildingpulse_fdd
```
- Если шаблон пуст, скрипт честно выводит: `Quality Evaluation: NOT RUN`.
- При наличии экспертной разметки рассчитывает Recall релевантных статей и выводит полный список False Exclusions (критических ошибок отсева).

---

## 4. Запуск автоматических тестов

В проекте реализован полный набор тестов (unit, DB, idempotency, negative validation, E2E):

```powershell
pytest
```

Тестовый набор покрывает все 12 обязательных сценариев из `AGENT_TASK.md`:
1. Ровно 6 таблиц схемы; безопасный повторный `db init`.
2. Дедупликация OpenAlex/DOI без дублирования в `works` с сохранением `source_records`.
3. Корректный импорт статей без аннотации.
4. Экспорт 50 уникальных ID с проверкой SHA-256 `papers.jsonl`.
5. Повторный `next` до `accept` возвращает существующий пакет.
6. Приём валидного ответа создаёт ровно 50 `paper_reviews` и `status = committed`.
7. Повторный приём того же ответа безопасен (no-op, не дублирует отзывы).
8. Отклонение неверного `job_id` или хеша протокола.
9. Отклонение пропущенных, лишних и дублирующихся ID с конкретными сообщениями.
10. Отклонение сломанного JSON / лишних полей / ошибок типов с 0 частичных записей.
11. Переход к следующим 50 статьям после завершения предыдущего пакета.
12. Отмена задания возвращает статьи в пул без потери данных.

---

## 5. Переменные окружения (.env)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgrespassword@127.0.0.1:5434/research_engine` | Строка подключения к PostgreSQL |
| `OPENALEX_API_KEY` | *(пусто)* | Бесплатный API-ключ OpenAlex (снимает анонимные лимиты) |
| `OPENALEX_MAILTO` | `researcher@example.com` | Email для Polite Pool OpenAlex |
