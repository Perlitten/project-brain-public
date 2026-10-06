# Project Brain — карта для агентов

Что система делает и как поднять её локально — в [README.md](README.md). Здесь только то, что
экономит ходы: **куда идти под конкретную задачу**, границы слоёв и ловушки.

Масштаб для ориентира: 661 файл, ~80k строк Python (`brain/` 34.8k, `tests/` 22.9k, `apps/` 10.6k).
Читать целиком не нужно и не надо — нужно за один шаг попадать в нужные 2–3 файла.

## Спина навигации: роутер ↔ пакет

`apps/api/routers/<X>.py` почти всегда соответствует `brain/<X>/`. Нашёл одну сторону — знаешь вторую.

| Роутер | Ядро | Тесты |
|---|---|---|
| `routers/core.py` | `brain/search/`, `brain/database/` | `test_repo_scoped_search.py`, `test_vector_search.py` |
| `routers/web.py` (JSON для web UI) | `brain/embeddings/integrity.py`, `apps/api/helpers.py` | `test_web_router.py` |
| `routers/graph_v2.py` | `brain/graph/` | `test_graph_and_memory.py`, `test_graph_v3_identity.py` |
| `routers/impact.py` | `brain/insights/impact_*.py`, `brain/analyzers/` | `test_impact_analysis.py`, `test_impact_bounded.py` |
| `routers/drift.py` | `brain/insights/drift_*.py` | `test_drift_analyzer_v3.py`, `test_architecture_change_guard.py` |
| `routers/coupling.py` | `brain/insights/coupling_*.py` | `test_coupling_intelligence.py` |
| `routers/remediation.py` | `brain/insights/remediation_*.py` | `test_remediation_planner.py` |
| `routers/freshness.py` | `brain/freshness/` | `test_freshness_incremental.py`, `test_repo_freshness.py` |
| `routers/ledger.py` | `brain/ledger/` | `test_evidence_ledger.py` |
| `routers/workspace.py` | `brain/workspace/` | `test_workspace_registry.py` |
| `routers/harness.py` | `brain/database/harness_models.py`, `brain/memory/harness_store.py` | `test_harness_api.py`, `test_harness_store.py` |
| `routers/jobs.py` | `brain/workers/` | `test_worker_queue.py`, `test_worker_capacity_readiness.py` |
| `routers/lab.py` | `brain/lab/` | `test_change_laboratory.py` |
| `routers/experiments.py` | `brain/experiments/` | `test_remediation_experiments.py` |
| `routers/portfolio.py` | `brain/portfolio/` | `test_portfolio_graph.py` |
| `routers/control.py` | `brain/control/` | `test_control_plane.py` |

## Маршруты по типовым задачам

| Задача | Начинать с |
|---|---|
| Вёрстка/брендинг web UI | `apps/web/` (Next.js, деплоится отдельно на Vercel; API отдаёт только JSON) |
| Новый или изменённый эндпоинт | `apps/api/routers/<тема>.py` → `apps/api/schemas.py` → регистрация в `apps/api/main.py` |
| Health-check, поведение при недоступной БД | `apps/api/main.py`, `brain/database/session.py`, `tests/test_health.py` |
| Форма ответа API / DTO | `apps/api/schemas.py`, `brain/database/models.py` |
| Поиск по коду, ранжирование | `brain/search/code_search.py`, `lexical_ranking.py`, `similarity.py` |
| Сборка контекст-пака, бюджеты | `brain/context/context_pack_builder.py`, `budget.py`, `brain/search/code_search.py` |
| Ретривал-пайплайн, реранк | `brain/retrieval/pipeline.py`, `service.py`, `reranker.py` |
| Индексация репозитория | `brain/indexers/repo_indexer.py`, `file_indexer.py`, `symbol_extractors.py` |
| Эмбеддинги, pgvector | `brain/embeddings/store.py`, `pgvector_sql.py`, `integrity.py` |
| ColBERT / late interaction | `brain/late_interaction/provider.py`, `store.py`, `client.py` |
| Память, решения, правила | `brain/memory/decision_store.py`, `rule_store.py`, `relevance.py` |
| Скоуп репозитория и свежесть индекса | `brain/memory/repo_scope.py`, `repo_freshness.py`, `source_manifest.py` |
| MCP-инструменты | `apps/mcp_server/server.py` (локальный), `remote_server.py` (удалённый), `tests/test_mcp.py` |
| Фоновые задачи | `brain/workers/tasks.py`, `queue.py`, `worker.py` |
| Схема БД, миграции | `brain/database/models.py`, `harness_models.py`, `migrations.py` |
| Настройки, пути | `brain/config/settings.py`, `paths.py` |

## Точки входа

- **HTTP API** — `apps/api/main.py`, роутеры в `apps/api/routers/`.
- **CLI** — `brain` (`[project.scripts]`) → `apps/cli/main.py`, команды в `apps/cli/commands/`.
- **MCP** — `apps/mcp_server/server.py` и `remote_server.py`.
- **Воркер** — `brain/workers/worker.py`; очередь `queue.py`, задачи `tasks.py`.
- `brain/__main__.py` — тонкий шим на CLI, логики там нет.

## Границы слоёв

- `brain/` — ядро: логика, хранилища, анализ. **Не должно импортировать `apps/`.**
  Существующие исключения: 4 ленивых импорта `apps.api.helpers` внутри функций в
  `brain/insights/proactive.py` и шим `brain/__main__.py`. Новых не добавлять — выноси
  общий код в `brain/`, а не тяни транспорт в ядро.
- `apps/` — транспорт: HTTP, CLI, MCP. Бизнес-логике здесь не место.
- Хранилища: Postgres + pgvector (реляционка и векторы), Neo4j (граф, через
  `brain/graph/graph_client.py`), Redis (кэш). Сервисы — в `docker-compose.yml`.

## Команды

```bash
docker compose up -d                        # postgres, redis, neo4j, api, worker (+scheduler)
py -3 -m pytest tests/ -q                   # полный прогон ~9 мин — запускать детачем
py -3 -m pytest tests/test_health.py -q     # обычный цикл: один файл
py -3 -m ruff check .                       # line-length 120, select E,F,W
py -3 -m brain.insights.drift_cli check --repo . --base master --candidate HEAD
```

На Windows использовать `py -3`: системные `python` и `pip` в PATH рассинхронизированы.
Деплой — `deploy/RUNBOOK.md` и `deploy/server_up.sh`.

## Ловушки

- **`reports/` принадлежит контейнерам** (uid 999). Распаковка релизного tar на проде на нём
  всегда падает — это норма, а не сбой деплоя; исключать через `--exclude='reports'`.
  Не чейнить стамповку релиза через `&&` после `tar`.
- **`tests/conftest.py` только гасит внешнее окружение** (нейтрализует ключи из `.env`,
  которые флипают поведение — `PROJECT_BRAIN_API_KEY` и т.п.). Логики там нет: тесты по-прежнему
  сами поднимают настройки до импорта модулей
  приложения — отсюда `E402` в `per-file-ignores` для `tests/**`. Не «чини» порядок импортов.
- **В `brain/graph/` сосуществуют поколения**: `schema.py` / `schema_v2.py`, `builder_v2.py`,
  `extractors_v2.py`, `migration_v3.py`. Прежде чем править — определи, какое поколение
  реально используется вызывающим кодом.
- **`rules/golden_tasks.yaml` — контракт eval**, а не документация: «описание задачи →
  ожидаемые файлы и модули». Меняешь раскладку модулей — обнови и его, иначе eval начнёт
  врать. Новая типовая задача → новая запись там же (и строка в таблице маршрутов выше).
