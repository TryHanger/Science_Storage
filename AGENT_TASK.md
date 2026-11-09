# TASK FOR CODING AGENT — Research Engine MVP v0.1

**Роль:** самостоятельный coding-agent/исполнитель. **Приоритет:** работающий сквозной цикл, а не дизайн платформы. Реализуй, протестируй и предоставь проект в текущем запуске. Не останавливайся на архитектурном плане. Не требуй подтверждений на обычные технические решения. Не делай push/деплой на чужие серверы и не изменяй существующие проекты без разрешения.

## 0. Цель и границы

Рабочий цикл: **OpenAlex → 500–1000 записей → точная дедупликация → PostgreSQL → ZIP-пакет из 50 статей → ручной ChatGPT → results.json → строгая проверка → транзакционное сохранение 50 результатов → следующий пакет**.

Один научный вопрос в v0.1: **`buildingpulse_fdd`**, пример протокола в `contracts/research_protocol.md`. Протокол читается из файла и фиксируется SHA-256; в будущем можно использовать другой файл без переписывания кода. Гарантируем обработку всех записей *зафиксированной выборки*, не гарантируем полноту всего мирового научного корпуса.

**НЕ делать:** UI, Google Drive, Zotero, PDF/fulltext, RAG, embeddings, MCP, ASReview, evidence registry, Discovery Loop, граф цитирований, микросервисы, авторизацию, API модели, фоновый планировщик. Только локальный CLI. `additional_references` от GPT сохранять в принятом исходном ответе для будущей версии, не импортировать в `works`.

## 1. Стек

Python 3.11+, PostgreSQL 16/17 (локальный `docker compose` с bind только `127.0.0.1`), `psycopg` 3, `httpx`, `pydantic` 2, `typer`, `pytest`. Минимум зависимостей. Можно использовать простой SQL без ORM. `.env.example`, `requirements.txt`/`pyproject.toml`, README с командами для PowerShell, SQL-инициализация, тесты.

Разработай модульный монолит с файлами `research_engine/{cli,db,openalex,normalize,queue,contracts,validator}.py` (или близко). БД на мощной машине локально для пилота; переменная `DATABASE_URL` позже позволяет подключиться к слабому ПК. Не открывать PostgreSQL в Интернет; ключ OpenAlex только в ENV, никогда в ZIP/логах.

## 2. Шесть таблиц PostgreSQL

Используй `contracts/schema.sql` как исходный контракт (можно добавить индексы/check constraints, но не новые сущности без веской причины):

1. `works` — UUID PK, нормализованный DOI, OpenAlex ID, title, abstract, publication_year, first_author, timestamps. Уникальность DOI/OpenAlex ID; пустые DOI — NULL.
2. `search_runs` — параметры поискового вызова, статус, `next_cursor`, fetched/inserted/duplicates/error, временные метки.
3. `source_records` — связь `work_id` + `search_run_id`, исходный JSONB от OpenAlex, `provider` и `provider_record_id`. Сохранять provenance при повторных запросах.
4. `review_jobs` — UUID, `study_key`, неизменяемый текст протокола и sha256, состояние `exported|repair_required|committed|cancelled`, sha256 принятого ответа и время. **Не терять контекст задания**.
5. `review_job_items` — точное множество `work_id` в job, `ordinal` и snapshot записи, выданной GPT.
6. `paper_reviews` — `job_id + work_id` UNIQUE, решение `include|exclude|uncertain`, `evidence_level`=`title_only|abstract_only`, `needs_fulltext`, `reason`, `study_key`, sha256 протокола, время. Ни один review не перезаписывать молча.

В v0.1 один активный job на (`study_key`, `protocol_sha256`) и последовательная работа, без параллельных worker. Выбор следующего пакета исключает уже закоммиченные работы и работы в незавершённых jobs; строго упорядоченный `ORDER BY works.id` (или по `created_at,id` со стабильным tie-break). Создание пакета и резервирование ID — одна транзакция. При повторном `next` незавершённый пакет переиспользуется, а не создаётся новый.

## 3. Импорт OpenAlex

Реализовать `research import-openalex --query "fault detection HVAC" --limit 500 [--year-from 2018]`.

Требования:
- использовать официальный `/works` API, `search`, `per_page<=100`, `cursor=*`/`meta.next_cursor` при необходимости; free key в `OPENALEX_API_KEY`. Не встраивать секреты;
- нормально обрабатывать `429` и `Retry-After`, временные ошибки, retry/backoff, лимиты запросов, сохранение `next_cursor` в `search_runs`, возобновление после падения; завершать только когда целевое число уникально просмотренных записей/лимит достигнуто, иначе честный статус ошибки/неполной выгрузки;
- восстанавливать `abstract_inverted_index` в обычный текст, не выдумывать отсутствующую аннотацию;
- DOI приводить к нормализованному виду, OpenAlex ID к одному каноническому виду;
- дедуп: совпавший DOI или OpenAlex ID связывать с существующим `works`; fuzzy совпадение названия не объединять автоматически (можно логировать кандидатов);
- хранить исходный JSON в `source_records`; импорт повторного поискового запроса идемпотентен относительно `works`;
- выдавать понятную статистику найдено/новых/дубликатов/ошибок.

Если API-ключ/сеть недоступны, предоставить offline fixture и продемонстрировать полный pipeline на нём; **не выдавать fixture за реальный OpenAlex-импорт**. Реальные загрузки не должны неожиданно расходовать платные средства.

## 4. CLI — самый короткий ручной цикл

Обязательные команды:

```powershell
research db init
research import-openalex --query "fault detection HVAC" --limit 500
research next --study buildingpulse_fdd --protocol contracts/research_protocol.md --count 50
research accept --file exchange/incoming/results.json
research status --study buildingpulse_fdd --protocol contracts/research_protocol.md
```

`next`: создаёт `exchange/outgoing/<job_id>.zip`, печатает путь к нему и короткий готовый текст для вставки в ChatGPT. В ZIP строго:
- `manifest.json` — `schema_version`, `job_id`, `study_key`, `protocol_sha256`, `paper_count`, `work_ids`, `papers_sha256`, `generated_at`;
- `papers.jsonl` — одна JSON-строка на работу с `work_id`, `title`, `doi`, `publication_year`, `abstract`, `openalex_id`;
- `research_protocol.md` — неизменный протокол;
- `instructions.md` — инструкция AI: проверить каждый ID, оценить только title/abstract, не притворяться, что прочитан fulltext, вернуть файл;
- `response_schema.json` — точный JSON Schema.

`accept`: читает JSON-ответ, определяет `job_id`, проверяет его по данным БД и сохранённым снимкам, затем **одной DB-транзакцией** добавляет все `paper_reviews` и переводит job в `committed`. Сырой ответ сохраняет в `exchange/accepted/<job_id>/results.json` без редактирования. Точное расположение исходного ZIP неизменно после выдачи. Ответы никогда не оцениваются по позиции массива — только по `work_id`.

Если есть незакрытый job, `next` возвращает его путь, не создавая второй; `status` показывает total/pending/exported/committed/errors. Добавь простую команду `research cancel --job <uuid>` для освобождения зависшего job (не удаляет файлы и историю).

## 5. Формат результата — строгий контракт

См. `contracts/response_schema.json` и `contracts/example_results.json`. Корень:

```json
{
  "schema_version": "1.0",
  "job_id": "UUID",
  "protocol_sha256": "64 hex chars",
  "results": [
    {"work_id":"UUID", "decision":"include|exclude|uncertain",
     "evidence_level":"title_only|abstract_only", "needs_fulltext":true,
     "reason":"Конкретное обоснование"}
  ],
  "additional_references": [
    {"title":"...", "doi":null, "url":"https://...", "why_relevant":"..."}
  ]
}
```

`additional_references` необязателен (считать отсутствующее поле пустым массивом), хранить в сыром JSON. Не превращать предложения модели в публикации БД автоматически. Чужие поля запрещены; порядок `results` произвольный.

**Validator должен сообщать конкретно:** невалидный JSON с позицией, неизвестное поле и путь к нему, отсутствующие/лишние/повторные `work_id`, несовпадение `job_id`, `protocol_sha256` или `schema_version`, неправильный тип, неверное число записей, попытка принимать отменённую или чужую партию. При ошибке — 0 новых `paper_reviews`, DB не повреждена; сырой ответ и `validation_errors.json` идут в `exchange/rejected/...`; stderr + nonzero exit. Предусмотреть понятный ремонтный текст `repair_prompt.txt`, который можно вставить в GPT для повторной генерации полного JSON.

Повторное принятие идентичного ответа => безопасный no-op и сообщение «already accepted»; другая версия ответа для `committed` => конфликт, **без перезаписи**. Проверять импорт в одной транзакции. Сквозная проверка cardinality = все ожидаемые IDs ровно один раз.

## 6. Эталонный набор и качество

Поставь `gold_set_template.csv` (минимум колонки `work_id_or_doi`, `expected_decision`, `reason`, `labeler`) и скрипт `research evaluate-gold --file ... --study ...`, который рассчитывает recall релевантных работ и список false exclusions. Сами 30–40 настоящих эталонных статей должен независимо разметить пользователь/исследователь: **не придумывать DOI и labels, не выдавать синтетические фикстуры за научный gold standard**. Если эталон не заполнен, честно пометить проверку качества как `NOT RUN`.

## 7. Тесты и обязательная демонстрация

Писать pytest и интеграционные тесты с временной PostgreSQL DB. Отдельно проверить:
1. Созданы ровно 6 проектных таблиц; повторный `db init` безопасен.
2. Дубликат OpenAlex/DOI не создаёт новую `works`, `source_records` сохраняет источник.
3. Без `abstract` импорт работает.
4. `next` из 50 создаёт ZIP с 50 **разными** ID и корректным хешем `papers.jsonl`.
5. `next` второй раз до `accept` не резервирует новые 50.
6. Тестовый ответ с 50 валидными ID даёт ровно 50 reviews/`committed`.
7. Повтор того же ответа не меняет количество reviews.
8. Неправильный `job_id`/хеш протокола отклоняется.
9. Missing, extra, duplicate IDs отклоняются с точными сообщениями.
10. Сломанный JSON/неизвестные поля/несоответствие типов отклоняются, 0 частичных записей.
11. Обработав первую партию, следующий `next` берёт следующие 50.
12. Отмена/повтор после отказа не приводит к потере записей.

**Доказать реальный локальный E2E** с тестовым `results.json`, явно маркированным как synthetic, не как фактический анализ ChatGPT. Если доступен OpenAlex, отдельно показать реальный импорт 500–1000 записей и статистику. Руководство должно объяснять, как пользователь выполняет **настоящий** ручной шаг в ChatGPT.

## 8. Готовность и артефакты

Готово, когда на чистой машине по README можно поднять PostgreSQL, импортировать записи или fixture, создать 50-публикационный ZIP, принять тестовый JSON, получить 50 reviews и следующий ZIP, а все негативные тесты пройдены.

Финально предоставить:
- рабочий исходный код и SQL (не только описание);
- `README.md` для Windows PowerShell с точными командами, `.env.example`, `compose.yaml`;
- образец полного job ZIP и валидного JSON; `response_schema.json`;
- `pytest`-результат, E2E-лог, вывод `research status`, статистику фактического импорта (либо честный blocker);
- `git status`, `git diff --stat`, зафиксированные версии зависимостей, SHA-256 ключевых файлов;
- один ZIP-пакет аудита с кодом, схемой, тестами, логами и инструкцией для независимой проверки. Без секретов и приватных PDF.

**Сроки в ответе не обещать. Не уходить в перепроектирование. Не считать реализацию завершённой без работающего локального E2E.**

## 9. Обновлённые официальные источники для реализации

- OpenAlex API: https://help.openalex.org/api/
- OpenAlex поиск: https://help.openalex.org/api/searching/
- Пагинация: https://help.openalex.org/api/paging/ — `per_page` до 100, cursor + API key
- Аутентификация / Retry-After: https://help.openalex.org/api/authentication/

Внимание: OpenAlex может менять правила API, используй актуальные ответы и ошибки. Для MVP не подключай дополнительные источники, пока один полностью рабочий цикл не доказан.
