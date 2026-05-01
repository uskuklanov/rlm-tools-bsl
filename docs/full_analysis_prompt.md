# Full Analysis Prompt — E2E Test for All Helpers

Use this prompt to run a comprehensive analysis of a 1C document using all available helpers.
Replace `РеализацияТоваровУслуг` with your target object name, and `<path>` with the actual path to your 1C source code.

---

## Prompt

```
Мне нужно провести полный анализ документа РеализацияТоваровУслуг в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно знать:
- Структура документа: реквизиты, табличные части, формы, модули
- Процедуры и экспортные функции в модулях объекта и менеджера
- Кто вызывает ключевые процедуры (проведение, установка статуса)
- По каким регистрам делает движения
- Какие документы являются основанием и какие создаются на основании
- Подписки на события, регламентные задания, печатные формы
- Функциональные опции, роли и права доступа
- Значения связанных перечислений (статусы)
- В какие подсистемы входит
- Нетиповые доработки (кастомизации)
- Есть ли расширения и какие перехваты делают
- Метрики сложности кода
- Запросы в модуле менеджера
- Бизнес-логика проведения: как именно проводится документ, какие регистры затрагиваются и почему, цепочка вызовов от ОбработкаПроведения до записи в регистры
- Печатные формы: какие печатные формы доступны, через какие модули формируются

Начни с help() чтобы узнать доступные инструменты, затем используй их по своему усмотрению.
Обрати внимание на Step 0 — UNDERSTAND в стратегии и бизнес-рецепт, если он был предложен.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute)

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt exercises all 45 BSL helpers (v1.10.0) without explicitly naming them. The AI agent discovers the toolset via `help()` and decides which helpers to use. Business questions in the prompt trigger `_BUSINESS_RECIPES` injection via `get_strategy()` (v1.3.5+).

| Area | Expected helpers |
|------|-----------------|
| Navigation | `find_module`, `find_by_type`, `safe_grep`, `search`, `search_methods` |
| Code analysis | `extract_procedures`, `find_exports`, `read_procedure`, `extract_queries`, `code_metrics`, `find_call_hierarchy` |
| Call graph | `find_callers_context`, `find_call_hierarchy` |
| XML parsing | `parse_object_xml`, `find_enum_values` |
| Business analysis | `analyze_object`, `analyze_document_flow`, `analyze_subsystem` |
| Composite | `get_object_full_structure`, `analyze_object`, `analyze_document_flow` |
| Customizations | `find_custom_modifications`, `detect_extensions`, `find_ext_overrides` |
| Infrastructure | `find_register_movements`, `find_register_writers`, `find_based_on_documents`, `find_event_subscriptions`, `find_scheduled_jobs`, `find_print_forms`, `find_functional_options`, `find_roles` |
| Integration (v1.4.0) | `find_http_services`, `find_web_services`, `find_xdto_packages`, `find_exchange_plan_content` |
| Strategy | Step 0 UNDERSTAND + business recipe (проведение/печать/интеграция/перечисления/ввод на основании/структура объекта) via `get_strategy(query=...)` |
| Help | `help` |

## Recommended settings

- **effort**: `high` (default since v1.1.0) — gives 50 execute calls, enough for full coverage
- **max_output_chars**: `30000` — large modules produce verbose output
- **execution_timeout_seconds**: `120` — composite helpers on large configs need time
- **Индекс обязателен для слабых моделей** — подробности в [HELPERS.md](HELPERS.md#индекс-и-слабые-модели)

## Test results (v1.2.0, ERP 23K+ files, 617K methods index)

### Without index

| Client | Model | rlm_execute | Sections | Notes |
|--------|-------|------------|----------|-------|
| Claude Code | Sonnet 4.6 | 52 | 16 | Reference quality, ~14.6 min |
| Cursor | Sonnet 4.6 | 24 | 15 | Near-reference quality, dense batching |
| Kilo Code | Minimax m2.5 | 19 | 14 | Gaps: wrong enum, no callers, timeouts |

### With index

| Client | Model | rlm_execute | Sections | Notes |
|--------|-------|------------|----------|-------|
| Claude Code | Sonnet 4.6 | 35 | 15 | 33% fewer calls, ~11 min, FTS used |
| Kilo Code | Minimax m2.5 | 10 | 14 | Huge improvement: clean report, correct data |

---

# Integration Analysis Prompt — E2E Test for v1.4.0 Helpers

Use this prompt to verify the new integration metadata helpers added in v1.4.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format).

---

## Prompt

```
Мне нужно провести полный анализ интеграционных возможностей конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно знать:

1. **HTTP-сервисы (REST API)**:
   - Полный список HTTP-сервисов с корневыми URL
   - Для каждого: шаблоны URL, доступные HTTP-методы (GET/POST/PUT/DELETE), обработчики
   - Какие из них типовые (БСП), какие кастомные
   - Статистика: сколько всего сервисов, шаблонов, методов

2. **Веб-сервисы (SOAP)**:
   - Полный список веб-сервисов с namespace
   - Для каждого: операции, параметры операций, типы возвращаемых значений, процедуры-обработчики
   - Статистика: сколько сервисов, операций

3. **XDTO-пакеты**:
   - Полный список пакетов с namespace
   - Для пакетов с типами: objectType и valueType с их свойствами
   - Какие пакеты относятся к обмену данными, какие к интеграции с внешними системами
   - Статистика: сколько пакетов, сколько из них с типами

4. **Планы обмена**:
   - Список всех планов обмена (через find_by_type)
   - Для основного плана обмена (например, ОбменУправлениеПредприятием): полный состав — какие объекты входят и с каким режимом авторегистрации
   - Регламентные задания, связанные с обменом (фильтр по 'Обмен|Exchange|Синхрониз|Загруз|Выгруз')

5. **Связи между компонентами**:
   - Какие HTTP-сервисы используют XDTO-пакеты (по namespace)
   - Какие веб-сервисы ссылаются на XDTO-типы
   - Общие модули, связанные с интеграцией (поиск по 'Интеграц|Обмен|Exchange')

Начни с help('http') и help('обмен') чтобы узнать доступные рецепты и инструменты.
Затем используй find_http_services(), find_web_services(), find_xdto_packages(), find_exchange_plan_content() и другие хелперы.

Дай итоговую сводку со всеми цифрами в виде структурированного отчёта. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt specifically targets the 4 new integration helpers from v1.4.0 and verifies they work correctly on real 1C configurations. It also tests the integration business recipe and alias routing.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| HTTP services | `find_http_services()` | name, root_url, templates with methods |
| Web services | `find_web_services()` | name, namespace, operations with params |
| XDTO packages | `find_xdto_packages()` | name, namespace, types (EDT only) |
| Exchange plans | `find_exchange_plan_content(name)` | ref, auto_record for each object |
| Exchange plan list | `find_by_type('ExchangePlans')` | BSL modules of exchange plans |
| Related jobs | `find_scheduled_jobs()` + filter | jobs related to exchange/sync |
| Integration recipe | `help('http')`, `help('обмен')` | recipe displayed correctly |
| Strategy injection | `get_strategy(query='интеграция')` | BUSINESS RECIPE injected |
| Index version | `rlm_start` warnings | no version warning with v6 index |

## Expected results on ERP 2.5 (EDT, ~20K BSL modules)

| Metric | Expected range |
|--------|---------------|
| HTTP services | 20–30 |
| Web services | 15–20 |
| XDTO packages | 250–350 |
| XDTO packages with types | 200+ (EDT format) |
| Exchange plans | 5–15 |
| Exchange-related scheduled jobs | 10–30 |

---

# Object Synonyms Prompt — E2E Test for v1.4.1 Helpers

Use this prompt to verify the new object synonym search and index info helpers added in v1.4.1.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Requires index v7+ (`rlm-bsl-index index build <path>`).

---

## Prompt

```
Мне нужно проверить возможности поиска объектов по бизнес-именам (синонимам) в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: версию индекса, имя конфигурации, наличие FTS и синонимов
   - Если builder_version < 8 или has_synonyms = False — сообщи, что нужно перестроить индекс

2. **Поиск по бизнес-именам (кириллица)**:
   - search_objects('себестоимость') → какие документы, регистры, модули связаны с себестоимостью
   - search_objects('расчет') → проверка кириллического case-insensitive поиска (должен найти "Расчет...")
   - search_objects('авансовый') → найти документ "Авансовый отчет"
   - search_objects('номенклатура') → справочники и регистры, связанные с номенклатурой

3. **Поиск по категориям**:
   - search_objects('общий модуль') → все CommonModules (через категорийный префикс)
   - search_objects('регистр сведений') → все InformationRegisters
   - search_objects('документ') → должно вернуть много документов

4. **Дифференциация search_objects от search_methods**:
   - search_objects('Себестоимость') → объекты 1С (документы, регистры, модули)
   - search_methods('Себестоимость') → процедуры/функции в коде
   - Объясни разницу: search_objects = ЧТО за объект, search_methods = ГДЕ в коде

5. **Комбинация с другими хелперами**:
   - Найди через search_objects объект по бизнес-имени
   - Затем используй его техническое имя (object_name) в find_module(), find_by_type(), parse_object_xml()
   - Покажи цепочку: бизнес-имя → техническое имя → структура объекта → код

6. **Статистика**:
   - Общее количество объектов с синонимами (search_objects('') с limit=10000)
   - Распределение по категориям: сколько Documents, Catalogs, CommonModules, InformationRegisters и т.д.
   - Топ-5 самых длинных синонимов

Начни с get_index_info() для проверки доступности, затем help('search_objects') для рецепта.
Обрати внимание на NOTE в WORKFLOW: search_objects = WHAT object? search_methods = WHAT code?

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.4.1 (`search_objects`, `get_index_info`), the Cyrillic case-insensitive UDF, 4-level ranking, category prefix search, and the workflow differentiation between `search_objects` and `search_methods`.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version=8, has_synonyms=True |
| Synonym search | `search_objects(query)` | Finds objects by Russian business name |
| Cyrillic case | `search_objects('расчет')` | Finds "Расчет..." despite case mismatch |
| Category search | `search_objects('общий модуль')` | Returns only CommonModules |
| Empty query | `search_objects('')` | Returns all objects (with limit) |
| Differentiation | `search_objects` vs `search_methods` | Objects vs code methods |
| Chaining | `search_objects` → `find_module` → `parse_object_xml` | Business name → technical name → structure |
| Help recipe | `help('search_objects')` | Recipe displayed correctly |
| WORKFLOW note | Strategy Step 1 | NOTE about search_objects vs search_methods |
| Ranking | Exact name first | `search_objects('АвансовыйОтчет')` → exact match rank 0 |

## Expected results on ERP 2.5

Verified on EDT (ЕРП 2.5.7, 20K modules, 17 218 synonyms) and CF (ЕРП 2.5.14, 23K modules, 13 661 synonyms).

| Metric | EDT (actual) | CF (actual) | Expected range |
|--------|-------------|------------|---------------|
| Total synonyms | 17,218 | 13,661 | 12,000–18,000 |
| CommonModules | 3,301 | 3,909 | 3,000–4,500 |
| InformationRegisters | 1,391 | 1,313 | 1,200–1,800 |
| Enums | 1,434 | 247 | 200–1,500 |
| Reports | 1,052 | 1,098 | 800–1,200 |
| Catalogs | 1,018 | 1,041 | 800–1,200 |
| Documents | 642 | 685 | 600–800 |
| AccumulationRegisters | 221 | 217 | 200–500 |
| Categories covered | 32 | 29 | 29–32 |
| search_objects('себестоимость') hits | 10–30 | 10–30 | 10–30 (across categories) |
| get_index_info().builder_version | 8 | 8 | 8 |
| DB size | 966.5 MB | 1,137.6 MB | 950–1,150 MB |
| Build time (full) | ~466s | ~630s | 400–650s |

---

# Regions & Module Headers Prompt — E2E Test for v1.4.2 Helpers

Use this prompt to verify the code regions search and module header search helpers added in v1.4.2.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Requires index v8+ (`rlm-bsl-index index build <path>`).

---

## Prompt

```
Мне нужно исследовать структуру кодовой базы конфигурации ERP через области кода и заголовки модулей.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: версию индекса, has_regions, has_module_headers
   - Если builder_version < 8 — сообщи, что нужно перестроить индекс

2. **Поиск областей кода по бизнес-теме**:
   - search_regions('Проведение') → найти все области, связанные с проведением документов
   - Сгруппируй результаты по category: сколько в Documents, сколько в CommonModules
   - Выбери 3 документа с областью "Проведение" и покажи диапазоны строк (line-end_line)
   - search_regions('Себестоимость') → области расчёта себестоимости, в каких объектах

3. **Анализ нетиповых доработок через заголовки модулей**:
   - search_module_headers('++') → найти модули с маркерами кастомизации
   - Для каждого найденного: покажи category, object_name и текст маркера
   - Сколько всего модулей помечены маркером "++"?

4. **Обнаружение аннотаций и метаданных модулей**:
   - search_module_headers('@strict-types') → модули с EDT-аннотациями
   - search_module_headers('подсистема') → модули с описанием принадлежности подсистеме
   - Сколько модулей имеют описательные заголовки (не аннотации)?

5. **Комбинация с другими хелперами**:
   - Найди через search_regions('Проведение') документ с большой областью проведения (end_line - line > 200)
   - Затем extract_procedures() на этом модуле — покажи процедуры внутри области проведения
   - Используй find_register_movements() для этого документа — покажи регистры
   - Цепочка: область кода → процедуры → движения по регистрам

6. **Статистика**:
   - Общее количество областей в индексе (search_regions('') с limit=1, но get_index_info покажет)
   - Топ-10 самых частых имён областей (search_regions('') с большим limit, группировка по name)
   - Сколько модулей имеют заголовочные комментарии

Начни с get_index_info() для проверки доступности, затем help('search_regions') для рецепта.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.4.2 (`search_regions`, `search_module_headers`), the `category` field in results, Copyright filtering in headers, and the workflow of combining region discovery with code analysis helpers.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version=8, has_regions=True, has_module_headers=True |
| Region search | `search_regions(query)` | Finds #Область by name substring, returns category |
| Header search | `search_module_headers(query)` | Finds modules by header comment, no Copyright noise |
| Cyrillic case | `search_regions('проведение')` | Finds "Проведение" despite case mismatch |
| Empty query | `search_regions('')` | Returns all regions (with limit) |
| Customization markers | `search_module_headers('++')` | Finds modules with `++` modification markers |
| EDT annotations | `search_module_headers('@strict-types')` | Finds annotated modules |
| Chaining | `search_regions` → `extract_procedures` → `find_register_movements` | Region → code → business flow |
| Help recipe | `help('search_regions')` | Recipe displayed correctly |
| Category in results | `search_regions('Проведение')` | Each result has `category` (Documents, CommonModules, etc.) |

## Expected results on ERP 2.5

Verified on EDT (ЕРП 2.5.7, 20K modules) and CF (ЕРП 2.5.14, 23K modules).

| Metric | EDT (actual) | CF (actual) | Expected range |
|--------|-------------|------------|---------------|
| Total regions | 88,756 | 100,873 | 85,000–105,000 |
| Total module_headers | 1,299 | 1,402 | 1,000–1,500 |
| search_regions('Проведение') hits | 1,228 | 1,298 | 1,200–1,400 |
| search_regions('Себестоимость') hits | 89 | 87 | 80–100 |
| search_module_headers('++') hits | 178 | 217 | 150–250 |
| search_module_headers('@strict-types') hits | 47 | 156 | 40–160 |
| search_module_headers('подсистема') hits | 206 | 240 | 200–250 |
| Copyright headers in table | 0 | 0 | 0 (filtered) |
| get_index_info().has_regions | True | True | True |
| get_index_info().has_module_headers | True | True | True |
| DB size | 966.5 MB | 1,137.6 MB | 950–1,150 MB |
| Build time (full) | ~466s | ~630s | 400–650s |

---

# Extension Overrides Prompt — E2E Test for v1.5.0 Helpers

Use this prompt to verify the extension overrides indexing and enrichment helpers added in v1.5.0.
Replace `<path>` with the actual path to your 1C source code that has nearby extensions (e.g. `src/cf/` with `src/cfe/` siblings). Requires index v9+ (`rlm-bsl-index index build <path>`).

---

## Prompt

```
Мне нужно провести полный анализ перехватов расширений в конфигурации.
Путь: <путь к каталогу исходников 1С, рядом с которым есть расширения>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса и расширений**:
   - Вызови get_index_info() и выведи: builder_version, has_extension_overrides, extension_overrides (количество)
   - Если builder_version < 9 — сообщи, что нужно перестроить индекс для поддержки перехватов
   - Проверь extension_context из ответа rlm_start — какая роль конфигурации, есть ли расширения рядом

2. **Обзор всех перехватов из индекса**:
   - get_overrides() без фильтров → сколько всего перехватов, из каких расширений, source="index" или "live"
   - Сгруппируй по расширениям: для каждого расширения — количество перехватов и назначение (purpose)
   - Сгруппируй по типам аннотаций: сколько &Перед, &После, &Вместо, &ИзменениеИКонтроль

3. **Прицельный поиск перехватов**:
   - Выбери объект с наибольшим количеством перехватов
   - get_overrides('ИмяОбъекта') → все перехваты этого объекта
   - Для каждого перехвата: метод, тип аннотации, метод расширения, файл расширения

4. **Обогащение extract_procedures**:
   - Найди модуль перехваченного объекта через find_module()
   - extract_procedures(path) → проверь, что у перехваченных методов есть поле overridden_by
   - Выведи: имя метода, тип, строка, и для перехваченных — данные из overridden_by (расширение, аннотация, метод расширения)

5. **Чтение тела метода с перехватом**:
   - Выбери перехваченный метод из п.4
   - read_procedure(path, method_name) → только оригинальное тело (регрессия: без include_overrides по умолчанию)
   - read_procedure(path, method_name, include_overrides=True) → оригинал + тело расширенного метода с аннотацией
   - Сравни два вызова: второй должен содержать секцию "=== Перехвачен &Аннотация в расширении..."

6. **Сравнение index vs live**:
   - get_overrides('ИмяОбъекта') → source должен быть "index" (мгновенно, из SQLite)
   - detect_extensions() + find_ext_overrides(ext_path, 'ИмяОбъекта') → live-данные (с диска)
   - Результаты должны совпадать по количеству перехватов

7. **Статистика**:
   - Количество перехваченных объектов (уникальные object_name)
   - Количество перехваченных методов (уникальные target_method)
   - Количество расширений
   - Процент методов с source_module_id (привязанных к исходному модулю)

Начни с get_index_info() и help('override') для рецепта.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the extension overrides indexing pipeline from v1.5.0: the `get_overrides()` helper with index/live fallback, `extract_procedures` enrichment with `overridden_by`, `read_procedure(include_overrides=True)` for reading extension method bodies, and the live scan in `rlm_start` fast-path.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version=9, has_extension_overrides=True, count>0 |
| Extension context | `rlm_start` response | extension_context with nearby extensions, live overrides |
| Indexed overrides | `get_overrides()` | source="index", instant response, all overrides |
| Filtered overrides | `get_overrides(object_name)` | Correct filtering by object |
| Procedure enrichment | `extract_procedures(path)` | overridden_by field on intercepted methods |
| Read original | `read_procedure(path, name)` | Clean body without override data (regression) |
| Read with overrides | `read_procedure(path, name, include_overrides=True)` | Original + extension body with annotation header |
| Live fallback | `detect_extensions()` + `find_ext_overrides()` | Live data matches index data |
| Help recipe | `help('override')` | Recipe displayed correctly |
| WORKFLOW Step 5 | Strategy | get_overrides, read_procedure(include_overrides), overridden_by |

## Expected results on ciam_bgu (CF, 14K modules, 1 extension)

| Metric | Actual | Expected range |
|--------|--------|---------------|
| builder_version | 9 | 9 |
| extension_overrides (total) | 16 | 10–200 |
| Extensions detected | 1 | 1+ |
| source_module_id linked | 16/16 (100%) | >90% |
| target_method_line populated | 15/16 (94%) | >80% |
| Annotation types | После (majority) | Перед/После/Вместо/ИзменениеИКонтроль |
| get_overrides() source | "index" | "index" (with v9 index) |
| read_procedure(include_overrides=True) | Original + extension body | Must contain "=== Перехвачен" section |
| read_procedure() default | Original only | No override data (regression check) |
| Overhead on build time | <0.5s | <1s for 1 extension |

---

# Unified Search Prompt — E2E Test for v1.5.1 Helper

Use this prompt to verify the unified `search()` helper added in v1.5.1.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Requires index v8+ with FTS and synonyms (`rlm-bsl-index index build <path>`).

---

## Prompt

```
Мне нужно проверить возможности единого поиска search() в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Broad-first discovery**:
   - search('себестоимость') → выведи ВСЕ результаты
   - Для каждого: source_type, text, object_name, path, path_kind
   - Сколько различных source_type получено? Должны быть минимум method + object (а лучше все 4)
   - search('проведение') → аналогично, покажи diversity результатов

2. **Per-source quota**:
   - search('а', limit=30) → группировка по source_type: сколько от каждого источника
   - Ни один source_type не должен занимать более 7-8 записей (квота = limit // 4)
   - search('а', limit=8) → группировка — покажи, как квота работает при малом limit

3. **Scope фильтрация**:
   - search('себестоимость', scope='methods') → все source_type == 'method', path_kind == 'bsl'
   - search('себестоимость', scope='objects') → все source_type == 'object', path_kind == 'metadata'
   - search('Проведение', scope='regions') → все source_type == 'region', path_kind == 'bsl'
   - search('подсистема', scope='headers') → все source_type == 'header', path_kind == 'bsl'
   - Для каждого scope покажи количество результатов

4. **Browse mode (пустой query + конкретный scope)**:
   - search('', scope='objects', limit=20000) → полный список объектов (parity с search_objects('', limit=20000))
   - search('', scope='regions', limit=200) → список областей
   - search('') → пустой список (scope='all' + пустой query)
   - Покажи количество в каждом случае (учти, что без явного limit=... вернётся максимум 30)

5. **path_kind семантика**:
   - Из результатов search('себестоимость'): выбери один с path_kind='bsl' и один с path_kind='metadata'
   - Для bsl-результата: прочитай модуль через read_file(path) — должен быть BSL-код
   - Для metadata-результата: прочитай через read_file(path) — должен быть XML/.mdo
   - Подтверди, что path_kind корректно отражает тип файла

6. **detail — доступ к оригинальным полям**:
   - Из результата с source_type='method': detail['rank'], detail['line'], detail['end_line'], detail['is_export']
   - Из результата с source_type='object': detail['category'], detail['synonym']
   - Из результата с source_type='region': detail['line'], detail['end_line'], detail['category']
   - Из результата с source_type='header': detail['header_comment'], detail['category']
   - Все detail-поля должны быть доступны

7. **Цепочка broad → precise**:
   - search('себестоимость') → находим объект (source_type='object')
   - Берём object_name из результата → find_module(object_name) → модули
   - extract_procedures(module_path) → процедуры
   - Цепочка: broad search → precise follow-up через специализированные хелперы

8. **Сравнение с прямыми вызовами**:
   - search('себестоимость', scope='methods') vs search_methods('себестоимость')
   - search('себестоимость', scope='objects') vs search_objects('себестоимость')
   - Совпадает ли количество результатов? (может отличаться из-за квоты limit)
   - При одинаковом limit результаты должны совпадать по содержанию

9. **Валидация**:
   - Попробуй search('тест', scope='invalid') → должен вернуть ValueError
   - Попробуй search('', scope='all') → пустой список

10. **Статистика** (передай limit=20000 для полного подсчёта):
    - search('', scope='objects', limit=20000): общее количество объектов с синонимами
    - search('', scope='regions', limit=200000): общее количество областей
    - search('', scope='headers', limit=20000): общее количество заголовков
    - Для methods пустой query не работает (FTS by design)

Начни с get_index_info() для проверки, затем help('search') для рецепта.
Обрати внимание: search() — broad-first shortcut; search_methods/search_objects/search_regions/search_module_headers — precise follow-up.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the unified `search()` helper from v1.5.1: broad-first discovery with per-source quota, scope filtering, browse mode parity, `path_kind` semantics, `detail` field access, and the broad-to-precise chaining workflow.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Broad discovery | `search(query)` | Multiple source_types in results (diversity) |
| Per-source quota | `search(query, limit=N)` | No source_type exceeds `max(limit//4, 3)` entries |
| Scope: methods | `search(q, scope='methods')` | All source_type='method', path_kind='bsl' |
| Scope: objects | `search(q, scope='objects')` | All source_type='object', path_kind='metadata' |
| Scope: regions | `search(q, scope='regions')` | All source_type='region', path_kind='bsl' |
| Scope: headers | `search(q, scope='headers')` | All source_type='header', path_kind='bsl' |
| Browse mode | `search('', scope='objects')` | Non-empty list, parity with search_objects('') |
| Empty all | `search('')` | Empty list |
| path_kind | Read files by path | bsl=BSL code, metadata=XML/.mdo |
| detail field | Access detail['rank'], detail['line'] etc. | All original fields present |
| Broad-to-precise | `search()` -> `find_module()` -> `extract_procedures()` | Chaining works end-to-end |
| Parity | `search(q, scope='X')` vs `search_X(q)` | Same results at same limit |
| Validation | `search(q, scope='invalid')` | ValueError raised |
| Help recipe | `help('search')` | Recipe displayed correctly |
| Strategy | WORKFLOW Step 1 | search() as broad-first, specialized as follow-up |

## Expected results on ERP 2.5

| Metric | Expected range |
|--------|---------------|
| search('себестоимость') source_types | 2-4 distinct types |
| search('себестоимость') total hits | 10-40 |
| search('а', limit=30) max per source_type | <= 7-8 (quota = 30//4) |
| search('себестоимость', scope='methods') | 5-20 methods |
| search('себестоимость', scope='objects') | 5-30 objects |
| search('Проведение', scope='regions') | 30 (default limit); 1,200-1,400 with limit=2000 |
| search('', scope='objects', limit=20000) count | 12,000-18,000 |
| search('', scope='regions', limit=200000) count | 85,000-105,000 |
| search('', scope='all') | [] (empty) |
| search(q, scope='invalid') | ValueError |
| path_kind='bsl' paths | End with .bsl |
| path_kind='metadata' paths | End with .xml or .mdo |

---

# Form Analysis Prompt — E2E Test for v1.6.0 Helpers

Use this prompt to verify the form XML parsing helper added in v1.6.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Requires index v10+ (`rlm-bsl-index index build <path>`).

---

## Prompt

```
Мне нужно провести полный анализ форм документа РеализацияТоваровУслуг в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: builder_version, has_form_elements, form_elements_count
   - Если builder_version < 10 — сообщи, что нужно перестроить индекс

2. **Обзор форм объекта**:
   - parse_form('РеализацияТоваровУслуг') → список всех форм документа
   - Для каждой формы: имя формы, количество обработчиков, количество команд, количество атрибутов
   - Какая форма самая сложная (больше всего обработчиков)?

3. **Детали обработчиков событий**:
   - Выбери форму с наибольшим количеством обработчиков
   - Сгруппируй обработчики по scope: сколько form-level, ext_info-level, element-level
   - Для element-level обработчиков: покажи element_name, element_type, event, handler, data_path
   - Какие события самые частые (OnChange, StartChoice, Selection)?

4. **Обратный поиск: процедура → элемент**:
   - Из формы, выбранной на шаге 2, возьми module_path и вызови extract_procedures(module_path) → найди ПриСозданииНаСервере
   - parse_form('РеализацияТоваровУслуг', handler='ПриСозданииНаСервере') → к чему привязана
   - Покажи scope и event найденной привязки
   - Выбери другой обработчик (element-level) и покажи: к какому элементу, какому событию, какому реквизиту привязан

5. **Команды формы**:
   - Из результата parse_form: выведи все команды формы (commands)
   - Для каждой: имя команды → процедура-действие (action)
   - Найди процедуру-действие в модуле формы через extract_procedures() — есть ли она?

6. **Атрибуты формы и DynamicList**:
   - Из атрибутов формы: найди основной атрибут (main=True) — какой тип объекта
   - Если есть атрибут типа DynamicList: покажи main_table и query_text (начало запроса)
   - Атрибут DynamicList привязан к табличному элементу формы — найди его в handlers по data_path

7. **Цепочка: форма → код → бизнес-логика (через module_path)**:
   - forms = parse_form('РеализацияТоваровУслуг') → выбери ФормаДокумента
   - Найди обработчик ПередЗаписью или ПослеЗаписи (scope="ext_info") в handlers
   - read_procedure(form['module_path'], handler_name) → прочитай тело обработчика (module_path уже в ответе parse_form!)
   - find_callers_context(handler_name) → кто ещё вызывает эту процедуру
   - find_register_movements('РеализацияТоваровУслуг') → регистры, куда пишет документ
   - Покажи связь: событие формы → обработчик (через module_path) → бизнес-логика → регистры

8. **Статистика из индекса**:
   - get_index_info() → form_elements_count
   - parse_form('') с пустым именем не должен работать (проверка валидации)
   - Сколько всего обработчиков, команд, атрибутов в form_elements_count

9. **CommonForms (если доступны)**:
   - Попробуй найти общие формы: search_objects('общая форма') или find_by_type('CommonForms')
   - Если найдена общая форма — вызови parse_form('ИмяОбщейФормы') и покажи её обработчики
   - У CommonForms: category='CommonForms', object_name=form_name=ИмяФормы, module_path указывает на Module.bsl если есть

Начни с get_index_info() для проверки, затем help('события формы') для рецепта.
Обрати внимание: parse_form() возвращает grouped-структуру по формам с handlers/commands/attributes внутри.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the form XML parsing helper from v1.6.0: `parse_form()` with grouped output, `scope` field semantics, reverse handler lookup, EDT/CF command extraction, DynamicList attributes, CommonForms support, and the form→code→business-logic chaining workflow.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version=10, has_form_elements=True, count>0 |
| Form overview | `parse_form(object_name)` | Grouped output: handlers/commands/attributes per form |
| Handler scope | `parse_form()` handlers | scope="form", "ext_info", "element" — no empty strings |
| Reverse lookup | `parse_form(handler='...')` | Finds binding for a BSL procedure |
| Commands | `parse_form()` commands | name→action mapping (CF and EDT) |
| DynamicList | `parse_form()` attributes | main_table, query_text (≤512 chars) |
| CommonForms | `parse_form('CommonFormName')` | Works for top-level common forms |
| Chaining | `parse_form` → `read_procedure` → `find_callers_context` → `find_register_movements` | Form event → handler → business logic → registers |
| Help recipe | `help('события формы')` | Recipe displayed correctly |
| Strategy | WORKFLOW Step 1 | parse_form listed |
| INSTANT | `parse_form()` source | Instant from index (has_form_elements=True) |

## Expected results on ERP 2.5

| Metric | Expected range |
|--------|---------------|
| builder_version | 10 |
| form_elements_count (CF) | ~250,000 |
| form_elements_count (EDT) | ~210,000 |
| Forms for РеализацияТоваровУслуг | 2–5 (ФормаДокумента + ФормаСписка + ...) |
| Handlers in ФормаДокумента | 20–60 |
| Commands in ФормаДокумента | 5–20 |
| Attributes in ФормаДокумента | 5–30 |
| scope="form" handlers | 3–8 per form (OnCreateAtServer, NotificationProcessing, ...) |
| scope="ext_info" handlers | 2–5 per form (AfterWrite, OnReadAtServer, ...) |
| scope="element" handlers | 10–50 per form (OnChange, StartChoice, ...) |
| CommonForms total | 300–600 |
| DB size overhead | +60–73 MB (+6.5%) |
| Build time overhead | +6–11 с (+1.5–2.6%) |

---

# Attribute Types & Predefined Items Prompt — E2E Test for v1.7.0 Helpers

Use this prompt to verify the new `find_attributes()` and `find_predefined()` helpers added in v1.7.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Requires index v11+ (`rlm-bsl-index index build <path>`).

**Background:** Before v1.7.0, answering "What type is subconto X?" required 7 `rlm_execute` calls (2 errors), manual `Predefined.xml` search, and parsing 810-line XML. Now it takes 1 call.

---

## Prompt

```
Мне нужно проверить новые возможности поиска реквизитов с типами и предопределённых элементов в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и проверь: has_object_attributes = True, has_predefined_items = True
   - Выведи object_attributes_count и predefined_items_count
   - Если builder_version < 11 — сообщи, что нужно перестроить индекс

2. **Поиск реквизитов по имени**:
   - find_attributes('Организация') → реквизиты из разных документов, справочников, регистров
   - find_attributes('Контрагент') → все реквизиты с именем Контрагент
   - find_attributes('Сумма') → проверка что возвращает тип (Number) и объект-владелец
   - Для каждого результата покажи: object_name, attr_name, attr_type, attr_kind

3. **Реквизиты конкретного объекта**:
   - find_attributes(object_name='РеализацияТоваровУслуг') → ВСЕ реквизиты документа с типами
   - Сгруппируй по attr_kind: attribute, ts_attribute (колонки ТЧ)
   - Покажи для ТЧ (ts_attribute): ts_name + attr_name + attr_type
   - find_attributes(object_name='ТоварыОрганизаций', kind='dimension') → только измерения регистра

4. **Предопределённые элементы**:
   - find_predefined('РеализуемыеАктивы') → тип субконто за 1 вызов (ключевой сценарий!)
   - find_predefined(object_name='ВидыСубконтоХозрасчетные') → все предопределённые субконто
   - find_predefined('Рубль') или find_predefined('USD') → предопределённые валюты
   - Для каждого покажи: item_name, item_synonym, types, item_code

5. **Поиск через search()**:
   - search('Организация', scope='attributes') → реквизиты
   - search('Реализуемые активы', scope='predefined') → предопределённые по синониму
   - search('Контрагент') → scope='all' должен включить attribute и predefined в результатах
   - Проверь, что source_type содержит 'attribute' и 'predefined'

6. **Бизнес-сценарий — тип субконто**:
   - Задача: "Какого типа субконто Реализуемые активы?"
   - Используй бизнес-рецепт: help('тип реквизита')
   - Выполни рецепт: find_predefined('РеализуемыеАктивы') → types
   - Сравни: раньше это требовало 7 вызовов, теперь — 1

7. **Статистика**:
   - Общее количество проиндексированных реквизитов (object_attributes_count)
   - Общее количество предопределённых элементов (predefined_items_count)
   - Распределение реквизитов по kind: attribute, dimension, resource, ts_attribute
   - Распределение реквизитов по category: Documents, Catalogs, InformationRegisters и т.д.
   - Топ-5 объектов с наибольшим количеством реквизитов

Начни с get_index_info() для проверки доступности, затем help('тип реквизита') для рецепта.
Обрати внимание: find_attributes() и find_predefined() — мгновенные (из индекса), не требуют чтения XML.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.7.0 (`find_attributes`, `find_predefined`), type normalization, search integration, business recipe routing, and the key use case that motivated the feature.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version=11, has_object_attributes=True, has_predefined_items=True |
| Attribute search by name | `find_attributes(name='X')` | Finds across all indexed categories, returns normalized types |
| Attribute search by object | `find_attributes(object_name='X')` | All attributes including TS columns |
| Attribute search by kind | `find_attributes(kind='dimension')` | Filters by attribute/dimension/resource/ts_attribute |
| Predefined by name | `find_predefined(name='X')` | Finds predefined items with types |
| Predefined by object | `find_predefined(object_name='X')` | All predefined of a chart/catalog |
| Search integration | `search(query, scope='attributes')` | New source_type='attribute' in results |
| Search integration | `search(query, scope='predefined')` | New source_type='predefined' in results |
| Synonym search | `search('Реализуемые активы', scope='predefined')` | Finds by Russian synonym |
| Business recipe | `help('тип реквизита')` | Recipe with find_predefined + find_attributes |
| Key scenario | `find_predefined('РеализуемыеАктивы')` | 1 call instead of 7 |
| Type normalization | attr_type field | `["CatalogRef.X"]` not `"cfg:CatalogRef.X"` |
| Help | `help('attributes')`, `help('predefined')` | Recipes displayed correctly |

## Expected results on ERP 2.5

| Metric | Expected range |
|--------|---------------|
| object_attributes_count | 38,000–73,000 |
| predefined_items_count | 2,000–4,500 |
| builder_version | 12 |
| Attributes of РеализацияТоваровУслуг | 200–250 (attributes + ts_attributes across all ТЧ) |
| Predefined of ВидыСубконтоХозрасчетные | 50–80 |
| find_attributes('Организация') hits | 470–500+ (capped at 500, across all categories) |
| find_predefined('РеализуемыеАктивы') | 1–2 results with 2+ types (custom prefix may add a second) |
| search('Организация', scope='attributes') | 5+ results |
| Categories covered | Documents, Catalogs, InformationRegisters, AccumulationRegisters, ChartsOfCharacteristicTypes, AccountingRegisters |
| DB size overhead | +25–35 MB (+3–4%) |
| Build time overhead | +30–45s |

---

# Where-Used Analysis Prompt — E2E Test for v1.9.0 `find_references_to_object`

Use this prompt to verify the new reverse-index helper added in v1.9.0
(аналог конфигуратора «Найти ссылки → В свойствах», issue [#10](https://github.com/Dach-Coin/rlm-tools-bsl/issues/10)).
Replace `Справочник.ВидыПодарочныхСертификатов` with your target object and `<path>` with the real path.

---

## Prompt

```
Мне нужно найти все места использования справочника ВидыПодарочныхСертификатов в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP rlm-tools-bsl. Начни с help('references').

Нужно:
- Полный список ссылок с разбивкой по видам (by_kind)
- Отдельно: типы реквизитов других объектов (kinds=['attribute_type'])
- Отдельно: состав подсистем / планов обмена / функциональных опций
- Отдельно: владельцы (если есть), ввод на основании, связи по типу
- По каждой ссылке — путь и строка в XML (если доступна)
- Если partial=True — сообщи, что индекс устарел и нужен rebuild (rlm_index(action='build'))
- Сравни с тем, что показал бы конфигуратор в окне «Найти ссылки (В свойствах)»

Дай итоговую сводку: total, truncated, partial, by_kind. Сохрани отчёт своими инструментами.

## ВАЖНЫЕ ПРАВИЛА
1. Один rlm_execute должен батчить связанные find_references_to_object вызовы.
2. В конце вызови rlm_end.
```

---

## What it covers

Покрывает 18 видов ссылок (`ref_kind`):

| `ref_kind`                       | Источник                                                              |
|----------------------------------|------------------------------------------------------------------------|
| `attribute_type`                 | Тип реквизита/измерения/ресурса/колонки ТЧ другого объекта            |
| `subsystem_content`              | Объект в составе подсистемы                                           |
| `exchange_plan_content`          | Объект в составе плана обмена                                         |
| `functional_option_content`      | Объект в составе функциональной опции                                 |
| `event_subscription_source`      | Источник подписки на событие                                          |
| `role_rights`                    | Право в роли                                                          |
| `defined_type_content`           | Тип в составе ОпределяемогоТипа                                       |
| `characteristic_type`            | Тип в составе ПВХ                                                     |
| `owner`                          | Владелец справочника                                                  |
| `based_on`                       | Документ-основание                                                    |
| `main_form` / `list_form`        | MainForm / ListForm                                                    |
| `default_object_form` / `default_list_form` | DefaultObjectForm / DefaultListForm                          |
| `command_parameter_type`         | Тип параметра команды (объектной или CommonCommands)                  |
| `predefined_characteristic_type` | Тип у предопределённого вида характеристики                           |
| `choice_parameter_link`          | ChoiceParameterLinks (зарезервировано)                                |
| `link_by_type`                   | LinkByType (зарезервировано)                                          |

## Expected results (v1.9.0, ERP 24K+ files)

| Metric | Expected |
|--------|----------|
| `find_references_to_object('Справочник.ВидыПодарочныхСертификатов')` total | ≥ 47 (target 51 per issue #10) |
| `by_kind` keys | 6+ different kinds |
| Время первого вызова с индексом v12 | < 500 ms (target < 200 ms) |
| `partial` на индексе v12 | `false` |
| `partial` на индексе v11 (legacy) | `true`, live-fallback с тем же набором ссылок |
| `metadata_references` size on ERP | ~150–250K rows (~20–35 MB) |
| Build time overhead vs v11 | +30–60 sec на ERP (~+5%) |

---

# Reverse-Index Coverage Audit Prompt — E2E Test for v1.9.0 reverse-index breadth

Данный промпт нагружает **новый reverse-index** (`metadata_references`) шире чем точечный where-used: проходит по нескольким kinds сразу, проверяет раскрытие `ОпределяемогоТипа` и поведение `partial` flag. Используется для:
1. валидации новых kinds после фиксов парсера CF Owners (`xr:Item`) и `parse_command_parameter_type` (`<CommonCommand>` + `<v8:TypeSet>`);
2. регрессионной проверки `find_defined_types()` (примитивы должны сохраняться);
3. **проверки live-fallback** на проектах без индекса (или старого v11) — `partial=True` должно работать корректно.

Замените `<путь>` на путь к вашим исходникам 1С.

---

## Prompt

```
Мне нужно провести аудит покрытия reverse-index новой возможности «Найти ссылки → В свойствах»
для конфигурации <проект>.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP rlm-tools-bsl. Начни с help('references').

Сделай:

1. **Состояние индекса** — выполни search('') или get_index_info() и сообщи:
   - builder_version (12 = новый, 11 = старый, нет = индекс отсутствует)
   - размер metadata_references (если есть)
   - формат проекта (CF/EDT)

2. **Top-objects по числу ссылок** — найди один объект с большим числом ссылок (используй
   распространённые типа Catalog.Контрагенты, Catalog.Номенклатура, Catalog.Организации,
   Catalog.ФизическиеЛица). Для выбранного объекта:
   - find_references_to_object(...) → выведи total, partial, by_kind
   - первые 10 ссылок с указанием kind и used_in
   - проверь, что partial=False на индексе v12, partial=True на v11/без индекса

3. **DefinedType chain** — найди один ОпределяемыйТип. Сначала search('ОпределяемыйТип') или
   find_module — определи имя. Затем:
   - find_defined_types('ИмяТипа') → выведи раскрытый список types
   - проверь, что **примитивы** (Number, String, Boolean, Date) если они есть — НЕ потеряны
     (regression для CF: в первой v1.9.0 сборке примитивы дропались на indexed path)
   - find_references_to_object('DefinedType.ИмяТипа') → где этот ОпределяемыйТип используется

4. **Кросс-kinds покрытие** — для одного объекта (Catalog.Организации или аналога)
   собери find_references_to_object и проверь, что в by_kind присутствуют как минимум 4
   разных kind из списка: attribute_type, subsystem_content, exchange_plan_content,
   functional_option_content, role_rights, defined_type_content, owner, based_on,
   command_parameter_type. Сообщи, какие kinds есть и каких НЕ оказалось.

5. **Owner-references regression** — для CF-проекта:
   попробуй найти владельца хотя бы одного подчинённого справочника. Используй фильтр
   kinds=['owner']. Если результат пустой и проект — CF, это потенциально регрессия фикса
   парсера xr:Item (issue v1.9.0 round-2). Сообщи об этом.

6. **CommandParameterType coverage** — попробуй фильтр kinds=['command_parameter_type'] на
   произвольном объекте, который часто фигурирует в CommonCommands (например ExchangePlan.Х
   или DefinedType.Y). Если в проекте есть CommonCommands и результат 0 — потенциальная
   регрессия фикса <CommonCommand>+<v8:TypeSet>.

7. **Truncation/priority** — если total > limit (по умолчанию 1000), вызови с limit=10 и
   проверь, что:
   - truncated=True
   - возвращённые 10 ссылок относятся к высокоприоритетным kind (attribute_type,
     subsystem_content, exchange_plan_content) — это проверка SQL ORDER BY priority

Дай итоговую сводку:
- Всего total ссылок проанализировано (по всем шагам)
- Покрытые kinds (по всем объектам)
- partial=True/False для каждого вызова
- Любые подозрения на регрессию (нулевые результаты там где их быть не должно)

В конце ОБЯЗАТЕЛЬНО вызови rlm_end. Сохрани отчёт своими инструментами в текущий каталог.
```

---

## What it covers

| Шаг | Хелпер | Что проверяет |
|-----|--------|---------------|
| 1 | `get_index_info`/`search` | Версия индекса, размер metadata_references |
| 2 | `find_references_to_object` | total, by_kind, partial, базовая выдача |
| 3 | `find_defined_types`, `find_references_to_object` | DefinedType раскрытие, примитивы, обратные ссылки |
| 4 | `find_references_to_object` (без kinds) | Кросс-kind coverage (≥4 разных) |
| 5 | `find_references_to_object(kinds=['owner'])` | Регрессия CF Owners (`xr:Item` парсер) |
| 6 | `find_references_to_object(kinds=['command_parameter_type'])` | Регрессия CF CommonCommand parser |
| 7 | `find_references_to_object(limit=10)` | SQL ORDER BY priority — truncation сохраняет приоритет |

## Expected results

| Сценарий | partial | total | by_kind | Особое |
|----------|---------|-------|---------|--------|
| Новый индекс v12 (CF/EDT) | `False` | 100s–1000s | ≥4 kinds | owner и command_parameter_type ≠ 0 на CF |
| Старый индекс v11 | `True` (live fallback) | сравнимо | ≥4 kinds | сошлись с v12 (с точностью до line) |
| Без индекса | `True` (live fallback) | сравнимо | ≥4 kinds | dummy `seen_objects` дедуп должен исключить дубли |
| Truncation `limit=10` | — | оригинал ≥ 10 | первые 10 — высокоприоритетные | `truncated=True`, `by_kind` отражает полный набор |

**Regression checks** (если что-то из перечисленного провалится — это баг, требующий фикса):
- CF индекс с 0 owner refs → `xr:Item` парсер сломан
- CF индекс с 0 command_parameter_type refs (при наличии CommonCommands) → `<CommonCommand>` или `<v8:TypeSet>` парсинг сломан
- DefinedType с потерянными примитивами → indexed path в `_normalize_dt_type` сломан
- Дубли ссылок при `partial=True` на проекте с одновременным sibling+Ext layout → `seen_objects` дедуп сломан

---

# Aggregating Helpers Prompt — E2E Test for v1.10.0 Helpers

Use this prompt to verify the new aggregating helpers added in v1.10.0:
`get_object_full_structure`, `find_call_hierarchy`, обновлённые `find_register_movements` (is_postable),
`find_event_subscriptions` (event_filter + limit) и anti-duplicate hint в `rlm_execute`.

## Prompt

```
Мне нужно проверить новые агрегирующие хелперы и улучшения UX в v1.10.0.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl.

Проверь:

1. **Полная структура объекта (D1)**:
   - get_object_full_structure('РеализацияТоваровУслуг') → один вызов вместо 3-5
   - Сравни: время + содержимое vs ручная цепочка parse_object_xml + find_attributes + find_predefined + find_enum_values
   - Покажи: posting, len(attributes), tabular_sections с колонками, раскрытые перечисления, _meta.index_used

2. **Иерархия вызовов (D2)**:
   - find_call_hierarchy('ОбработкаПроведения', direction='callers', depth=2)
   - Покажи дерево по уровням; обрати внимание на одноимённые методы (список носителей)
   - find_call_hierarchy(direction='callees') → проверь что возвращает error-dict с hint (не traceback)

3. **Непроводимые документы (B1)**:
   - find_register_movements('ВходящееПисьмо') (или другой непроводимый)
   - Проверь что result содержит is_postable: False + hint
   - Сверь с подсказкой про find_event_subscriptions

4. **Фильтрация подписок (C1)**:
   - find_event_subscriptions('') → старый контракт list[dict] (default limit=None)
   - find_event_subscriptions('', limit=50) → новый dict {subscriptions, total, returned, has_more}
   - find_event_subscriptions('АвансовыйОтчет', event_filter=['BeforeWrite']) → точечный фильтр

5. **Path resolution (A1)**:
   - parse_object_xml('Documents/X.mdo') ('фейковый' путь, без подкаталога) → должен авто-нормализоваться
   - parse_object_xml('Documents/НеСуществует.mdo') → FileNotFoundError с подсказкой про директорию
   - НЕ должно быть мусорных кандидатов вида X.mdo/X.mdo.mdo

6. **Anti-duplicate hint (C3)**:
   - Вызови find_module('X') дважды с одинаковым именем в разных rlm_execute
   - В метаданных ответа должна появиться секция duplicates: [{call, prev_call, helper}]
   - Возвращаемые значения хелпера должны быть прежними (не меняются)

7. **read_procedure UX (A2/A3)**:
   - read_procedure(path, 'НесуществующаяПроцедура') → None (контракт сохранён)
   - В метаданных ответа sandbox-hint про XML-only / extract_procedures (если был FileNotFoundError)

8. **Disambiguation проверка**:
   - help('структура объекта') → должен дать get_object_full_structure
   - help('иерархия вызовов') → find_call_hierarchy
   - help('перечисления') → find_enum_values
   - help('ввод на основании') → find_based_on_documents

Дай итоговую сводку. Сохрани отчёт своими инструментами.
```

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. Если хелпер падает с ошибкой — записывай её и продолжай: цель промпта — пройти ВСЕ 8 пунктов.
5. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.

## What it covers

This prompt validates the new aggregating helpers and behavioural changes in v1.10.0. It exercises 8 distinct verification points:

| # | Verification | Helpers / behaviours |
|---|---|---|
| 1 | D1 — Object full structure (one call vs 3-5) | `get_object_full_structure` (`_meta.index_used`, `fallback_reason`, `ts_synonyms_available`) |
| 2 | D2 — Multi-level call hierarchy + error contract | `find_call_hierarchy(direction='callers')` tree, `truncated_targets`; `direction='callees'` returns structured error-dict |
| 3 | B1 — Non-postable documents | `find_register_movements` returns `is_postable: False` + hint when `Posting=Deny` |
| 4 | C1 — Event subscription filter + pagination | `find_event_subscriptions(event_filter=[...], limit=N)` — filtered list + paginated dict contract |
| 5 | A1 — Path resolution for fake `.mdo`/`.xml` | `parse_object_xml` normalises base; FileNotFoundError carries explicit hint; no `X.mdo/X.mdo.mdo` candidates |
| 6 | C3 — Anti-duplicate hint cross-execute | `rlm_execute` response includes `duplicates: [{call, prev_call, helper}]` for repeat calls; helper return value unchanged |
| 7 | A2/A3 — `read_procedure` UX | Returns `None` for missing procedure (contract preserved); sandbox hint about XML-only objects via `_add_error_hints` |
| 8 | Disambiguation via `help()` | `help('структура объекта')` → `get_object_full_structure`; `help('иерархия вызовов')` → `find_call_hierarchy`; `help('перечисления')` → `find_enum_values`; `help('ввод на основании')` → `find_based_on_documents` |

The prompt indirectly tests `_BUSINESS_RECIPES` bridge (G.5b), `_RECIPE_ALIASES` extension (G.4), DISAMBIGUATION section (S.3 / G.5), and the session-wide duplicate detection in `Sandbox._wrap_helpers` (1.5).

## Recommended settings

- **effort**: `high` (default since v1.1.0) — gives 50 execute calls, enough for the 8 verifications batched 1-2 per call
- **max_output_chars**: `30000` — paginated subscription dumps and call hierarchy trees can be verbose
- **execution_timeout_seconds**: `120` — `find_call_hierarchy(depth=2)` and aggregate helpers may need a few seconds
- **query**: `'структура объекта и иерархия'` — auto-injects relevant `_BUSINESS_RECIPES` (структура объекта, иерархия вызовов) into strategy

## Expected results

On a fresh v12 index, all 8 verifications should PASS. Common observations:

| Verification | Expected on v1.10.0 |
|---|---|
| D1 `get_object_full_structure('РеализацияТоваровУслуг')` | Single dict with `attributes`, `tabular_sections` (with synonyms via live enrichment), `predefined_items`, `enum_values_for_typed_refs`, `_meta.index_used=True` |
| D2 `find_call_hierarchy('ОбработкаПроведения', depth=2)` | `tree:[{name, callers:[...]}]`, `visited≥2`, `truncated_targets` populated for popular methods (>200 callers); `callees` → `{error, hint, supported_directions:['callers']}` |
| B1 non-postable document | When `Posting=Deny` is in XML: `is_postable: False`, `hint` mentions `find_event_subscriptions` |
| C1 `find_event_subscriptions` | Default → `list[dict]` (legacy contract); with `limit=N` → `{subscriptions, total, returned, has_more}`; `event_filter=['BeforeWrite']` returns server-side filtered subset |
| A1 fake `.mdo` path | `parse_object_xml('Documents/X.mdo')` auto-normalises to `Documents/X/X.mdo` (EDT) or `Documents/X/Ext/Document.xml` (CF); missing → FileNotFoundError with hint about directory |
| C3 anti-duplicate | Response contains `duplicates: [{call: seq, prev_call: seq_prev, helper: name}]` for cross-execute repeats; helper return value bit-for-bit identical |
| A2/A3 `read_procedure` | Missing procedure → `None`; missing path → FileNotFoundError; sandbox hint mentions `extract_procedures()` and XML-only objects |
| Disambiguation | All 4 `help()` queries route to expected helpers (after BUG-2 fix in v1.10.x: exact keyword match wins over substring) |

Real metrics from v1.10.0 e2e on Тест ЕРП (EDT, 2026-05-01): 8/8 verifications passed in 45 rlm_execute calls. EDT-specific note: documents that have **only** `<realTimePosting>Deny</realTimePosting>` without `<posting>` are still proper postable documents — `is_postable=False` triggers strictly on `<posting>Deny</posting>` (the operational-posting flag is orthogonal).

