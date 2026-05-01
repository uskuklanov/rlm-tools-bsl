# Changelog

## [1.9.4] — 2026-05-01

### Исправлено
- **Windows-служба не стартовала на чистой машине без предустановленного pywin32** ([#13](https://github.com/Dach-Coin/rlm-tools-bsl/issues/13)) — `pythonservice.exe` падал с `ModuleNotFoundError: No module named 'servicemanager'` (Event ID 14, Service Error 1053). В uv tool env `pythonservice.exe` запускается без `python.exe` рядом и без обработки `.pth`, поэтому `pywin32_bootstrap` не добавлял каталоги `win32/`, `win32/lib/`, `Pythonwin/` на `sys.path`. Фикс: helper `build_service_pythonpath()` теперь явно прописывает все четыре каталога в `PYTHONPATH`, который пишется в реестр службы (REG_MULTI_SZ `Environment`). На машинах с уже зарегистрированным системным pywin32 поведение не меняется. Для применения нужна переустановка службы — `simple-install-from-pip.ps1` (или `service uninstall && service install`).
- **Диагностический `diagnose-service-win.ps1 -RunDebug`** — путь до `pythonservice.exe` теперь читается из `ImagePath` в реестре (раньше искал в `Lib\site-packages\win32\` — этого пути нет в uv tool env, секция `12-debug-run` всегда скипалась с `not found`). PYTHONPATH для debug-запуска расширен до тех же 4 каталогов, что и в установленной службе — диагностика теперь воспроизводит реальное окружение службы.

## [1.9.3] — 2026-04-25

### Добавлено
- **Pointwise incremental refresh для метаданных в git fast path** — при изменении одного объекта в `Catalogs`, `Documents`, `InformationRegisters`, `AccumulationRegisters`, `AccountingRegisters`, `ChartsOfAccounts`, `EventSubscriptions`, `ScheduledJobs`, `XDTOPackages` индекс обновляется точечно (DELETE+INSERT по конкретному `object_name`) вместо category-wide rescan. На больших конфигурациях (Тест ERP, 1000+ объектов в категории) это основной источник времени инкрементального обновления. Категории вне whitelist (`Reports`, `Constants`, `FunctionalOptions`, `Subsystems`, `Roles`, `ChartsOfCharacteristicTypes` и др.) по-прежнему идут через bulk fallback — поведение строго эквивалентно полному rescan категории. Покрывает таблицы `object_attributes`, `predefined_items`, `object_synonyms`, `metadata_references`, `event_subscriptions`, `scheduled_jobs`, `xdto_packages`.
- **Trigger set split для `.command` файлов** — изменение `.command` (per-object Commands и CommonCommands) теперь корректно попадает в `metadata_trigger_set` (ранее игнорировалось), но НЕ в `obj_meta_changed` для synonym-ветки — чистая `.command`-дельта не вызывает category-wide synonym rescan (perf-фикс).
- **Telemetry git fast path** — одна INFO-строка после каждого incremental update: `pointwise refresh used for N categories (M objects), fallback used for K categories (reasons: {...}), bsl modules incremental: L`.
- **Soft fallback thresholds** — `_POINTWISE_MAX_OBJECTS=50` (абсолютный) и `_POINTWISE_MAX_RATIO=0.5` (доля от total в категории): при массовом изменении одного раздела точечный путь автоматически уступает bulk-collect (избегаем 50× per-object парсингов).
- **35+ новых регрессионных тестов** — `tests/test_pointwise_refresh.py` (32 теста) покрывает resolver, dispatcher, eligibility, equivalence with full build, регрессию synonym rescan на `.command`-дельте, Group B deletion с CF Ext layout. `tests/test_object_synonyms.py` дополнен 3 тестами на CF sibling-only layouts (top-level + nested Subsystems + dedup).

### Изменено
- **`_find_metadata_xml` и `_CMD_HOST_CATS`** вынесены из closure внутри `_collect_metadata_tables` на module level — переиспользуются pointwise-путём. Поведение существующего bulk collector не меняется.

### Исправлено
- **`_collect_object_synonyms` пропускал CF sibling-only layout** — категории, в которых объекты лежат прямо файлами без объектных подкаталогов (`Category/<Name>.xml` без `Category/<Name>/`), не индексировались в `object_synonyms`. Типичный кейс: EventSubscriptions в CF Документооборот КОРП — 229 строк в `event_subscriptions`, но 0 в `object_synonyms`, business-name search не находил их до первого incremental update. Фикс: второй проход по plain `.xml` в каталоге категории с дедупликацией через множество уже обработанных object_name. На Тест ЕРП (CF) после fresh build `object_synonyms` вырос с 13 661 до 18 078 (+4417), на Тест ЕРП (EDT) row-count не изменился (фикс не затрагивает EDT). Аналогичный sibling-only баг исправлен в `_collect_subsystems_recursive` для вложенных Subsystems. Тесты в [tests/test_object_synonyms.py](tests/test_object_synonyms.py).
- **Pointwise resolver не разрешал Group B категории и Subsystems** — `_resolve_object_from_path` использовал только `parse_bsl_path`, который знает категории из `format_detector.METADATA_CATEGORIES` (категории-хосты BSL-модулей). Чисто-метаданные категории — `EventSubscriptions`, `ScheduledJobs`, `FunctionalOptions`, `DefinedTypes` — там отсутствуют, поэтому resolver возвращал `(None, None)` для всех путей в этих категориях, и pointwise-dispatcher падал в bulk fallback с reason `no_objects`. Фикс: resolver делает fallback на первый сегмент пути с проверкой по `_CATEGORY_RU` (полный список indexable категорий), плюс корректно разбирает EDT layout `Category/<Name>/<Name>.mdo` через `parts[1]`. Тесты `test_event_subscriptions_cf_sibling`/`_edt`, `test_scheduled_jobs_cf_sibling`, `test_defined_types_cf_sibling` в [tests/test_pointwise_refresh.py](tests/test_pointwise_refresh.py).
- **Pointwise Group B оставлял stale `object_synonyms`** — EventSubscriptions/ScheduledJobs/XDTOPackages входят в `_SYNONYM_CATEGORIES` через `_CATEGORY_RU`, bulk collector их синонимы собирал, но `_refresh_global_object`/`_refresh_xdto_package` обновляли только глобальные таблицы и `metadata_references`, без `object_synonyms`. После pointwise synonym-ветка вычитала `pointwise_categories` из своего фильтра, поэтому category-wide synonym refresh тоже пропускался — синоним подписки/job/XDTO оставался устаревшим. Фикс: вызов `_insert_synonym_for_object` через `parse_metadata_xml` (для паритета с bulk collector) внутри Group B refresh + DELETE по `(category, object_name)` в начале.
- **Pointwise CoA эмитил `metadata_references`, которых full build не пишет** — `_refresh_object` вызывал `_insert_references_for_object` для всех whitelisted категорий, но full collector эмитит `parsed.references` только в loop по `_ATTR_CATEGORIES` (где CoA отсутствует — она только в `_PREDEFINED_CATEGORIES`). Это давало расхождение pointwise vs fresh full build по таблице `metadata_references` для CoA. Фикс: gating `if category in _ATTR_CATEGORIES:` перед `_insert_references_for_object`. Тест `test_coa_pointwise_matches_full_build` в [tests/test_pointwise_refresh.py](tests/test_pointwise_refresh.py).
- **Group B deletion path не чистил CF Ext layout и stale rows с `parsed.name != object_name`** — для Group B при deletion файла исходный код делал только `DELETE WHERE name=?` по `object_name`. Но bulk collector через `rglob` индексирует все три CF layouts (`Category/<obj>/<obj>.mdo`, `Category/<obj>.xml`, `Category/<obj>/Ext/*.xml`); если внутренний `<Name>` отличался от folder name (а row хранится с `name=parsed.name`), DELETE мискачивал. Фикс: deletion path расширен до `DELETE WHERE name=? OR file IN (candidate_files) OR file LIKE 'Category/<obj>/%' ESCAPE '\\'` — ловит любой layout. Аналогично для `metadata_references.path`. `_` и `%` в названиях экранируются через helper `_escape_for_sql_like`. Тест `test_event_subscription_cf_ext_deletion` в [tests/test_pointwise_refresh.py](tests/test_pointwise_refresh.py).

### Обратная совместимость
- Pointwise работает только в git fast path; полное построение и не-git update не затронуты.
- Pointwise молча пропускает DELETE/INSERT в `metadata_references`/`object_synonyms` на legacy-индексах без этих таблиц (`v1.9.0` и старше) — поведение строго эквивалентно bulk path.
- При SAVEPOINT-rollback (любое исключение в pointwise) категория автоматически уходит на bulk fallback — корректность не страдает, лишь логируется в reasons.

## [1.9.2] — 2026-04-25

### Исправлено
- **Расположение индекса при установке как Windows-служба** — `get_index_dir_root()` теперь учитывает `RLM_CONFIG_FILE` (зеркально к `_cache_base()` из v1.9.1): при service-install без явного `RLM_INDEX_DIR` индексы пишутся в `dirname(RLM_CONFIG_FILE)/index/`, а не в `system32/config/systemprofile/.cache/rlm-tools-bsl/` (LocalSystem). Прежнее обещание docstring «orthogonal to RLM_CONFIG_FILE» отменено
- **Авто-миграция legacy-индексов** — новая функция `migrate_legacy_index_root()`: при первом старте v1.9.2 каждая папка с `bsl_index.db` или `method_index.db`, лежащая в `~/.cache/rlm-tools-bsl/<hash>/`, переезжает в новый root. Идемпотентна, безопасна для desktop-юзеров (`legacy == new` → NOOP), не трогает индексы при заданном `RLM_INDEX_DIR`. Вызывается из server.py (startup), docker-entrypoint.sh (перед auto-update индексов) и cli.py (`build`/`update`/`info`/`drop`)

### Изменено
- **Объединение source-инсталлятора** — `simple-install.ps1` и `reinstall-service.ps1` (Windows, source) слиты в один идемпотентный `simple-install.ps1`. Подходит и для первичной установки, и для апгрейда (повторный запуск после `git pull`). Включает cleanup stale dist-info в global+user site-packages, `uv cache clean`, `--force --reinstall`, обновление глобального Python (для pythonservice.exe), верификация через `/health` (без открытия MCP-сессии)
- **Унификация cleanup-логики во всех install-скриптах** — `simple-install-from-pip.ps1` (Windows, PyPI), `simple-install.sh` (Linux, source), `simple-install-from-pip.sh` (Linux, PyPI) приведены к общему стандарту. Все четыре скрипта на upgrade теперь делают: остановку и uninstall существующей службы (best-effort, идемпотентно для fresh install), `uv cache clean rlm-tools-bsl`, `uv tool install --force --reinstall` (для PyPI-вариантов дополнительно `--upgrade`), верификацию через `/health` (вместо `/mcp`, не создаёт лишнюю MCP-сессию). Все четыре скрипта прокидывают `uv tool dir --bin` в PATH **до** проверки существующей установки (иначе при свежей PowerShell-сессии или после `uv tool update-shell` без re-source шелла существующая служба не определилась бы и stop/uninstall был пропущен). Linux-варианты дополнительно страхуются `systemctl --user disable --now rlm-tools-bsl.service` + `daemon-reload` от orphaned-юнитов. Windows-варианты дополнительно чистят stale `*rlm_tools_bsl*` из global+user site-packages (специфика pythonservice.exe, который грузится из глобального Python)
- **Документация** — `docs/INDEXING.md`, `docs/ENV_REFERENCE.md`, `docs/INSTALL.md` обновлены с новой precedence chain и Docker-рекомендациями (для Docker с `RLM_CONFIG_FILE` явно задавать `RLM_INDEX_DIR=/home/rlm/.cache/rlm-tools-bsl`, иначе индексы уедут в volume `rlm-config` вместо `rlm-index-cache`)

## [1.9.0] — 2026-04-19

### Добавлено
- **`find_references_to_object()`** ([#10](https://github.com/Dach-Coin/rlm-tools-bsl/issues/10)) — поиск всех ссылок на объект метаданных (аналог конфигуратора «Найти ссылки → В свойствах»). Покрывает 18 видов ссылок: `attribute_type`, `subsystem_content`, `exchange_plan_content`, `functional_option_content`, `event_subscription_source`, `role_rights`, `defined_type_content`, `characteristic_type`, `owner`, `based_on`, `main_form`, `list_form`, `default_object_form`, `default_list_form`, `command_parameter_type`, `predefined_characteristic_type` и др. Принимает русские/английские префиксы и Ref/Object/Manager-формы. Возвращает `{object, references, total, truncated, partial, by_kind}`.
- **`find_defined_types()`** — раскрытие `ОпределяемогоТипа` в список реальных типов (Catalog.X, Number и т.п.).
- **Индекс v12** — 4 новые таблицы: `metadata_references` (unified reverse-index, ~180K строк на ERP), `exchange_plan_content`, `defined_types`, `characteristic_types`. Category-aware DELETE для `metadata_references` при инкрементальном git fast path — сохраняет записи из неизменённых категорий.
- **Парсеры**: `canonicalize_type_ref()`, `parse_defined_type()`, `parse_pvh_characteristics()`, `parse_command_parameter_type()`. `parse_metadata_xml()` дополнен полем `references` (CF и EDT, паритет).
- **`DefinedTypes`** — добавлено в `_CATEGORY_RU` (отсутствовало в v11).
- Знание-база: новый рецепт `«ссылки»` (алиасы: references, where used, где используется, найти ссылки, поиск ссылок, в свойствах, вхождения).

### Обратная совместимость
- v12 требует rebuild (`rlm_index(action='build')`) или `update` — штатный `force_full_scan` при version mismatch автоматически наполнит новые таблицы.
- На индексе v11 `find_references_to_object` работает через live-fallback с `partial=true` (медленнее, но корректно).
- Все существующие `find_*`, `search`, `analyze_*`, `parse_*` — без изменений API. `parse_metadata_xml` добавляет поле `references`, существующие поля не трогаются.

## [1.8.2] — 2026-04-11

### Исправлено
- **detect_extensions() находит все расширения в контейнере** ([#7](https://github.com/Dach-Coin/rlm-tools-bsl/issues/7)) — ранее `_detect_single()` возвращала только первое расширение из каталогов-контейнеров (напр. `src/cfe/`). Новая функция `_detect_all()` собирает все конфигурации
- **find_callers_context не находит вызов из RecordSetModule** ([#9](https://github.com/Dach-Coin/rlm-tools-bsl/issues/9)) — `module_hint` в `get_callers()` ошибочно фильтровал вызывающих (caller) по `mod.object_name` вместо фильтрации по квалификации callee. Исправлен контракт: `module_hint` уточняет модуль определения метода, а не ограничивает вызывающих
- **Проверка формы в FS-fallback** — `"Form" in module_type` никогда не срабатывала; заменена на `form_name is not None`
- **Scope-выравнивание index/fallback** — для non-export и form-методов index path теперь сужает поиск callers до того же файла, как и FS-fallback

## [1.8.1] — 2026-04-10

### Исправлено
- **Docker на Windows: CRLF в entrypoint** — добавлен `.gitattributes` (`*.sh text eol=lf`), гарантирующий LF-окончания в shell-скриптах при checkout на любой платформе. Устраняет ошибку `exec /home/rlm/docker-entrypoint.sh: no such file or directory` при сборке образа на Windows с `core.autocrlf=true`

## [1.8.0] — 2026-04-06

### Добавлено
- **Git-ускоренное инкрементальное обновление** — `index update` автоматически использует `git diff` вместо полного обхода FS (`rglob + stat`), если каталог исходников внутри git-репозитория. E2E замеры: BSL-only update БГУ (14K модулей) — 17 сек, CRM (3K) — 5 сек vs ~5 мин полный скан. Прозрачный fallback на полный скан если git недоступен
- **Селективный refresh метаданных по категориям** — при git fast path пересобираются только таблицы метаданных затронутых категорий (EventSubscriptions, Catalogs, Roles и др.), а не все 10+ таблиц целиком
- **Dirty-снимок** — при каждом update сохраняется список dirty-файлов (staged/unstaged/untracked). При следующем update они принудительно включаются в дельту — переход dirty→clean не оставляет stale-данных
- **Best-effort для staged/unstaged/untracked diff** — таймаут этих команд не прерывает git fast path (критичен для Docker Desktop / Virtiofs). Пропущенные файлы подхватываются через dirty-снимок
- **Prefix-трансформация путей** — корректная работа когда `base_path` ≠ git root (EDT: `repo/src/`, CF: `repo/src/cf/`)
- **`git_fast_path`** в ответе `update()` — `true`/`false`, позволяет отличить способ обновления
- **`git_accelerated` + `git_head_commit`** в `index info` / `get_statistics()` — информация о git-ускорении
- **CLI `index info`** — строка `Git: да (коммит: abc12345)` / `нет (.git недоступен, был: ...)` / `нет`
- **Инкрементальный `file_paths`** — при git fast path без meta-изменений `file_paths` обновляется только для затронутых BSL-файлов
- **Docker: git в образе** — `python:3.12-slim` дополнен установкой `git` для поддержки git fast path в контейнере
- 44 новых теста git-утилит, fast path, fallback, dirty snapshot, selective metadata

## [1.7.4] — 2026-04-05

### Изменено
- **⚠️ MCP response shape change:** `rlm_index(build/update)` через MCP теперь возвращает `{"started": true, ...}` вместо финального результата. Построение выполняется в фоне, статус — через `rlm_index(action='info')`. CLI `rlm-bsl-index` не затронут

### Добавлено
- **Фоновое построение/обновление индексов** — `rlm_index(build/update)` запускается в фоне и сразу возвращает `{"started": true}`. Статус доступен через `rlm_index(action='info')` — поля `build_status`, `build_elapsed`, `build_result`/`build_error`
- **Защита от повторного запуска** — повторный build/update на тот же проект возвращает ошибку "already in progress" с elapsed time
- **Защита drop через MCP** — нельзя удалить индекс через MCP пока идёт построение
- **Защита info при build** — во время build `info` возвращает `build_status: "building"` без чтения полусобранной БД

## [1.7.3] — 2026-04-05

### ⚠ Breaking changes
- **`rlm_projects(action="add")`: `password` обязателен через MCP** — вызов без `password` возвращает `approval_required` вместо успешной регистрации. CLI и sync-функция `_rlm_projects()` не затронуты
- **`rlm_projects(action="remove/update/rename")`: `password` обязателен** — текущий пароль проекта для подтверждения. Без него операция возвращает `approval_required`
- **Семантика `password` в `update` изменена** — для защищённых проектов `password` = текущий пароль (auth). Смена пароля в два шага: `clear_password` с текущим паролем, затем `update(password="новый")` на legacy-проекте

### Добавлено
- **Пароль для всех мутирующих MCP-операций с проектами** — единый параметр `password` для add/remove/update/rename; сервер возвращает `approval_required` с non-secret полями исходного запроса (retry требует оригинальный запрос клиента)
- **Флаг `has_password`** — CRUD-ответы `rlm_projects` (list/add/remove/rename/update) содержат `has_password: true/false` для каждого проекта
- **Structured resolve errors** — `rlm_projects` wrapper возвращает `available_projects` при not found, `matches` при ambiguous, `"Did you mean '...'"` при fuzzy
- **Legacy-миграция** — проекты без пароля могут установить его через `update(name, password=...)`; остальные мутации заблокированы до установки пароля
- **Логирование** — `rlm_projects` и `rlm_index` логируют action/name/project (пароль маскируется `***`)
- 37 новых async-тестов MCP password enforcement, 7 тестов `has_password` в sanitized output

### Улучшено
- **`rlm_index(path=...)` для слабых моделей** — развёрнутое сообщение об ошибке: «проект не зарегистрирован, сначала спросите пользователя, регистрировать ли его»
- **`rlm_index` без пароля** — вместо `error` возвращает `approval_required: set_password` (единый формат с `rlm_projects`)

## [1.7.2] — 2026-04-04

### Исправлено
- **Пароль проекта для управления индексами** — build/update/drop через MCP теперь требуют зарегистрированный проект с паролем; пароль хранится как SHA-256 hash + salt; параметр `path` запрещён для деструктивных действий через MCP; CLI не затронут
- **BSL_PATTERNS** — добавлены английские ключевые слова (Procedure/Function/EndProcedure/EndFunction/Export); процедуры с английским синтаксисом теперь индексируются и парсятся
- **`_strip_code_line`** — исправлен порядок: строковые литералы удаляются до поиска `//`, URL в строках больше не обрезают код
- **`_check_health`** — HTTPError (500/404) теперь возвращает False; watchdog корректно перезапускает упавший сервер
- **IndexReader** — соединение SQLite закрывается при исключении в `_rlm_start`
- **`_load_env_file`** — символ `#` внутри кавычек больше не обрезает значение; inline-комментарий без кавычек только через ` #` (с пробелом). Экранированные кавычки внутри значений не поддерживаются
- **`find_attributes` / `find_predefined` indexed fallback** — пустой `[]` из индекса для `object_name='X'` (без категории) больше не блокирует live-fallback через find_module→XML
- **`find_attributes` fallback** — фильтрация по `attr_synonym` и `category`, применение `limit` — parity с indexed API
- **`find_predefined` fallback** — применение `limit`
- **Build lock `release()`** — ошибка `unlink()` в finally больше не маскирует исходное исключение

## [1.7.1] — 2026-04-03

### Исправлено
- docs/INDEXING.md — 5 неточностей (attr_kind, source_file, types_json, limit, CLI-примеры)

### Изменено
- Переименование method_index.db → bsl_index.db (ленивая миграция при первом обращении; fallback на старое имя если файл занят; повторная попытка при следующем обращении)
- Двухуровневый TTL сессий: 10 мин (idle, calls=0) / 30 мин (active, calls>0)
- Новые env: RLM_SESSION_TIMEOUT_IDLE, RLM_SESSION_TIMEOUT_ACTIVE
- Исправлен wiring: session_manager теперь подхватывает .env после load_project_env()

### Добавлено
- docs/MODULE_MAP.md — карта модулей и граф зависимостей
- docs/DEVELOPER_GUIDE.md — внутренние чеклисты (новая таблица / хелпер / рецепт)

## [1.7.0] — 2026-04-03

### Добавлено

- **Таблица `object_attributes`** — реквизиты, измерения, ресурсы и колонки ТЧ с типами для 6 категорий: Documents, Catalogs, InformationRegisters, AccumulationRegisters, ChartsOfCharacteristicTypes, AccountingRegisters
- **Таблица `predefined_items`** — предопределённые элементы с типами для Catalogs, ChartsOfCharacteristicTypes, ChartsOfAccounts
- **Хелпер `find_attributes()`** — мгновенный поиск реквизитов по имени, объекту, категории, типу (attribute/dimension/resource/ts_attribute). Параметр `limit` (по умолчанию 500)
- **Хелпер `find_predefined()`** — мгновенный поиск предопределённых элементов по имени или объекту. Параметр `limit` (по умолчанию 500)
- **XML-fallback для `find_attributes` и `find_predefined`** — без индекса работают через auto-resolve: `find_module(name)` → `category` → `f"{category}/{name}"` → live XML-парсинг. Те же паттерны что в `analyze_object` и других хелперах
- **`search()` расширен** — новые scope `"attributes"` и `"predefined"`, интеграция в `scope="all"`
- **Бизнес-рецепт "тип реквизита"** — автоматическое распознавание вопросов про типы субконто и реквизитов. Алиасы: "субконто", "тип субконто", "предопределённ", "attribute type"
- **`normalize_type_string()`** — нормализация типов из XML (xs:, cfg:, d4p1:) в читаемый JSON-массив
- **`parse_predefined_items()`** — парсинг предопределённых элементов (CF Predefined.xml и EDT .mdo)
- **Build lock** — эксклюзивная блокировка при build/update через OS-level file locking (`msvcrt.locking` на Windows, `fcntl.flock` на Linux). Реентрантная в рамках одного процесса. Автоматически освобождается при падении процесса
- **Confirm-механизм для MCP-тула `rlm_index`** — действия `build` и `drop` требуют подтверждения пользователя. AI-модель не может автоматически запустить построение индекса (5-10 минут, блокирует I/O); должна спросить пользователя и получить явное согласие. Причина: слабые модели (Grok, Kilo Auto) при обнаружении отсутствующего индекса самостоятельно вызывали `rlm_index(action='build')`, блокируя сервер на всё время построения
- **Стратегия без индекса** — секция `== INDEX ==` теперь всегда присутствует; без индекса содержит подробные hints: что работает через fallback, что возвращает пустые результаты, явный запрет на `rlm_index(action='build')`
- **Предупреждение в `RLM_START_DESCRIPTION`** — "NEVER call rlm_index(action='build') yourself"
- **E2E промпт #8** — "Attribute Types & Predefined Items" с expected ranges для ERP 2.5
- BUILDER_VERSION 10 → 11 (автоматический ребилд при обновлении)

### Изменения поведения

- **`search(scope='all')`** — `per_source` снижен с `limit // 4` до `limit // 6` (по 5 результатов каждого типа вместо 7 при `limit=30`) из-за 2 новых source_type
- **CLI `build`/`update`** — ловят `RuntimeError` от build lock и выводят сообщение вместо traceback
- **CLI `build`/`info`** — выводят счётчики Attributes, Predefined, Synonyms

### Исправления

- **Фикс парсера CF ТЧ** — `_cf_parse_attributes()` теперь ищет атрибуты табличных частей в `<ChildObjects>` (ранее возвращал пустой список для CF конфигураций)
- **`find_predefined` — `_strip_meta_prefix`** для входного `object_name` (убирает "Справочник.", "ПланВидовХарактеристик." и т.п.)

## [1.6.3] — 2026-04-01

### Добавлено

- **Docker-поддержка** — `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.example.yml` для развёртывания MCP-сервера в контейнере с авто-обновлением из PyPI, фиксацией версии через `RLM_VERSION` и graceful degradation при отсутствии сети
- **MCP-тул `rlm_index`** — полный паритет с CLI `rlm-bsl-index`: действия `build`, `update`, `info`, `drop` через MCP-протокол без shell-доступа к контейнеру
- **`RLM_PATH_MAP`** — автоматическая трансляция хостовых путей в контейнерные (Docker): пользователь указывает привычные пути хоста, сервер подменяет префикс
- **`docs/QUICKSTART.md`** — сценарии развёртывания (Windows/Linux/Docker) + пошаговый пример настройки VSCode + Kilo Code от нуля до первого вопроса

### Изменено

- **`docs/INSTALL.md`** — добавлена секция Docker (Вариант B)
- **`README.md`** — секция Docker, ссылка на QUICKSTART

## [1.6.2] — 2026-04-01

### Добавлено

- **Номера строк в MCP-сессиях** — хелперы `read_file()`, `read_files()`, `read_procedure()` и `grep_read()` в sandbox агента теперь возвращают код с абсолютными номерами строк (формат `42 | КодСтроки`), что устраняет ошибки позиционирования. Raw API фабрик `make_helpers()`/`make_bsl_helpers()` не изменён
- **Бенчмарк CF-формы** — добавлены `Form.xml` фикстура и сценарий `parse_form()` в `run_benchmark.py`

### Изменено

- **CONTRIBUTING.md** — добавлено правило bump версии в `uv.lock`

## [1.6.1] — 2026-03-31

### Исправлено

- **CF-формы: корректный путь к модулю** — `parse_form()` теперь правильно возвращает `module_path` для форм в формате CF: `Ext/Form/Module.bsl` вместо `Ext/Module.bsl`
  - Исправлены обе ветки логики: live path resolution и index-based grouping (`_group_form_rows`)
  - Обновлены тестовые фикстуры для соответствия реальной структуре CF-выгрузки (Closes #4)

## [1.6.0] — 2026-03-30

### Добавлено

- **Парсинг XML форм** — новый хелпер `parse_form(object_name, form_name='', handler='')` для извлечения обработчиков событий, команд и атрибутов форм
  - Авто-детект формата CF (`Ext/Form.xml`) и EDT (`Form.form`) по namespace
  - Grouped output по формам с `module_path` для перехода к коду модуля формы
  - Обработчики с `scope`: `form` (форменные), `ext_info` (типо-специфичные), `element` (элементные)
  - Команды формы: маппинг command → BSL-процедура (CF и EDT)
  - Атрибуты: тип, `main_table` для DynamicList, `query_text` (≤512 символов)
  - Обратный поиск: `handler='ПроцИмя'` → к какому элементу/событию привязана процедура
  - Поддержка CommonForms: `category='CommonForms'`, `object_name=form_name=ИмяФормы`
- **Таблица `form_elements`** в SQLite-индексе (Level-9, index v10)
  - Параллельный сбор: `ThreadPoolExecutor(min(os.cpu_count(), 8))` по аналогии с role_rights
  - 4 индекса: по `object_name`, `(object_name, form_name)`, `handler`, `kind`
  - Meta-ключи: `has_form_elements`, `form_elements_count`
  - Мягкий апгрейд v9→v10 через `update()`, привязка к `build_metadata`
- **IndexReader**: `get_form_elements(object_name, form_name, handler)` — raw rows query
- **Бизнес-рецепт `"события формы"`** — пошаговый рецепт анализа форм объекта
  - Алиасы: `"обработчики формы"`, `"элементы формы"`, `"кнопки формы"`
- **E2E промпт** для верификации v1.6.0 в `docs/full_analysis_prompt.md`

### Изменено

- **BUILDER_VERSION** = 10
- **WORKFLOW Step 1 DISCOVER** — добавлен `parse_form()` (для задач про формы/UI)
- **RLM_EXECUTE_DESCRIPTION** — `parse_form` в списке хелперов
- **Instant helpers** — `parse_form()` при наличии `form_elements` в индексе

### Тесты

- Добавлено 35 тестов: парсер CF/EDT, хелпер (fallback + indexed), индекс, parity, CommonForms
- Обновлены тесты `test_bsl_knowledge.py`: рецепт count=7, `_match_recipe("события формы")`

## [1.5.1] — 2026-03-29

### Добавлено

- **Универсальный поиск `search()`** — единый хелпер для broad-first discovery по методам, объектам (синонимам), областям и заголовкам модулей. Fan-out по существующим поисковикам с per-source квотой, graceful degradation при отсутствии таблиц. Параметр `scope` для фильтрации по типу источника (`methods`, `objects`, `regions`, `headers`). Browse mode для пустого query с конкретным scope

### Тесты

- Добавлено 20 тестов на `search()`: diversity, quota, scope, validation, graceful degradation, registration

## [1.5.0] — 2026-03-28

### Добавлено
- **Индексирование перехватов расширений** — таблица `extension_overrides` в SQLite-индексе (Level-8, index v9)
  - При индексировании основной конфигурации автоматически сканируются соседние расширения, перехваты связываются с исходными модулями/методами
  - `_collect_extension_overrides()` — коллектор с lookup исходного модуля по `rel_path` + fallback по `object_name`+`module_type`
  - Мягкий апгрейд: v8 индекс обновляется до v9 через `CREATE TABLE IF NOT EXISTS` при `update()`
- **IndexReader**: `get_extension_overrides()`, `get_overrides_for_path()`, `get_extension_overrides_grouped()`
- **Хелпер `get_overrides()`** — мгновенный запрос перехватов из индекса с live fallback на v8/без индекса
- **Обогащение `extract_procedures`** — поле `overridden_by` у перехваченных методов (из индекса)
- **`read_procedure(include_overrides=True)`** — дописывает тело расширенного метода с аннотацией и файловой ссылкой

### Изменено
- **Fast-path `rlm_start`** — live `detect_extension_context()` + `_auto_scan_overrides()` всегда, даже при кэшированном индексе. Перехваты в ответе `rlm_start` всегда актуальны
- **WORKFLOW Step 5** — упрощён: `get_overrides()`, `read_procedure(include_overrides=True)`, `extract_procedures.overridden_by`
- **BUILDER_VERSION** = 9

### Улучшено
- **CLI `rlm-bsl-index index build`** — выводит версию индекса (`Index: v9`) в сводке после построения
- **Документация** — требование к структуре репозитория (vanessa-bootstrap) для автодетекта расширений

### Тесты
- 24 новых теста extension_overrides (включая case-insensitive кириллица, early-exit meta), **635 всего**

## [1.4.5] — 2026-03-27

### Добавлено
- **Реестр проектов** — серверный реестр `имя → путь к исходникам 1С`, новый модуль `projects.py` с `ProjectRegistry`: CRUD, трёхуровневый resolve (exact → substring → Levenshtein), атомарная запись, `.bak`, валидация путей
- **MCP-тул `rlm_projects`** — list/add/remove/rename/update проектов в реестре
- **Параметр `project` в `rlm_start`** — альтернатива `path`, резолв имени проекта через реестр; `project_hint` для незарегистрированных путей; обработка `RegistryCorruptedError`
- **Резолв mapped drives** в `rlm_projects` для Windows-сервиса (Session 0)
- **Документация** — `docs/PROJECT_REGISTRY.md`, обновлены README, ENV_REFERENCE, `.env.example`

### Тесты
- 49 новых тестов (38 юнит + 11 интеграционных), **611 всего**, 78% покрытие

## [1.4.4] — 2026-03-27

### Исправлено
- **`service` extra опубликован в PyPI** — `pip install rlm-tools-bsl[service]` теперь корректно устанавливает `pywin32` на Windows. Ранее `service` был только в `[dependency-groups]` (локальная разработка), теперь также в `[project.optional-dependencies]`

## [1.4.3] — 2026-03-27

### Добавлено
- **Публикация в PyPI** — `pip install rlm-tools-bsl` теперь основной способ установки
- **Автоматическая публикация** — при пуше тега `v*` CI собирает wheel и публикует в PyPI через OIDC Trusted Publisher (без токенов)
- **TestPyPI workflow** — ручной запуск для проверки публикации перед релизом (`publish-testpypi.yml`)
- **PyPI-бейдж** в README

### Изменено
- **README.md** — секция «Установка из PyPI» добавлена как основной способ, установка из исходников вынесена в подсекцию
- **docs/INSTALL.md** — PyPI (pip/uv) как Вариант A, установка из исходников — Вариант B
- **pyproject.toml** — добавлены classifiers (`License`, `Operating System`), URL на Changelog
- **release.yml** — добавлен job `publish` с `pypa/gh-action-pypi-publish` и OIDC

### Инфраструктура (из коммитов после v1.4.2)
- CI: `PYTHONIOENCODING=utf-8` для бенчмарка на Windows
- Зрелость OSS-репо: гигиена, coverage-бейдж, benchmark, фикс `limit` в `search_regions`/`search_module_headers`
- CI: автоматическое создание GitHub Release при пуше тега `v*`
- Ruff линтер + форматтер, SECURITY.md, фикс сигнатуры `find_custom_modifications`
- Рефакторинг документации: README упрощён, docs разнесены по темам, CONTRIBUTING + бейджи

## [1.4.2] — 2026-03-26

### Добавлено
- **Области кода** — таблица `regions` в SQLite-индексе, `search_regions(query)` для поиска областей `#Область` по имени
- **Заголовки модулей** — таблица `module_headers` в SQLite-индексе, `search_module_headers(query)` для поиска модулей по заголовочному комментарию
- **Миграция v7→v8** — автоматический full rebuild при обнаружении старого индекса
- **Delta-cleanup** — очистка regions/module_headers при инкрементальном обновлении

### Изменено
- **`BUILDER_VERSION = 8`** — добавлены таблицы `regions` и `module_headers`
- **`get_statistics()`, `get_index_info()`, `get_strategy()` обновлены** — отражают новые таблицы и возможности

## [1.4.1] — 2026-03-25

### Добавлено
- **Поиск объектов по бизнес-имени** — `search_objects(query)` с кириллическим case-insensitive поиском через UDF `py_lower()`. Таблица `object_synonyms` (12-18K строк на ЕРП) с категорийным префиксом ("Документ: Авансовый отчет"). 4-уровневое ранжирование: exact name > prefix > synonym substring > category match
- **Метаданные индекса** — `get_index_info()` возвращает версию, конфигурацию, доступные возможности (FTS, синонимы)
- **Флаг `--no-synonyms`** — отключение сборки таблицы синонимов в CLI
- **`search_objects` в WORKFLOW** — Step 1 DISCOVER дополнен поиском по бизнес-именам с разграничением от `search_methods`
- **`search_objects` как первый шаг рецептов** — все 6 доменов бизнес-рецептов начинаются с поиска объекта по синониму
- **Версия индекса в стратегии** — `Index v7 | N synonyms → search_objects() available`

### Изменено
- **`BUILDER_VERSION = 7`** — добавлена таблица `object_synonyms` с двумя индексами
- **`get_statistics()` включает `object_synonyms`** — количество записей в таблице синонимов
- **v6→v7 миграция в `update()`** — при `has_synonyms` отсутствует в index_meta (v6 индекс), синонимы строятся по умолчанию

### Важно
- **Требуется перестроение индекса** — для работы `search_objects()` и `get_index_info()` необходимо перестроить индекс: `rlm-bsl-index index build <путь>`. При инкрементальном `update` на v6 индексе таблица `object_synonyms` создаётся автоматически

### Тесты
- 40 новых тестов: коллектор CF/EDT (8), builder (5), search_objects (10), хелперы (5), стратегия (3), EDT (2), incremental update (2), категории (4), статистика (1)
- Было: 476 (стабилизация v1.4.0), стало: **516**

## [1.4.0] — 2026-03-24

### Добавлено
- **Парсинг HTTP-сервисов** — `find_http_services(name='')` с indexed + fallback. Извлекает: имя, корневой URL, шаблоны URL, HTTP-методы, обработчики
- **Парсинг веб-сервисов (SOAP)** — `find_web_services(name='')` с indexed + fallback. Извлекает: имя, namespace, операции с параметрами, обработчики
- **Парсинг XDTO-пакетов** — `find_xdto_packages(name='')` с indexed + fallback. Метаданные для обоих форматов, типы — из EDT `.xdto`
- **Состав плана обмена** — `find_exchange_plan_content(name)` с fallback на glob + XML-парсинг. CF: `Ext/Content.xml`, EDT: inline в `.mdo`. Объекты + флаг AutoRecord. Фильтрация hint-строк от indexed glob
- **Интеграционный рецепт в стратегии** — BUSINESS RECIPE для запросов об интеграции/обмене. Пошаговый план анализа из атомарных хелперов. Alias-маршрутизация: "обмен", "синхрониз", "exchange" → "интеграция"
- **`code_hint` в рецептах** — готовый Python-сниппет для слабых моделей, инжектируется в стратегию блоком `Ready-to-use code`. Реализован для рецепта "интеграция", механизм расширяемый на все домены
- **Категории XDTOPackages и ExternalDataSources** — добавлены в `METADATA_CATEGORIES` + aliases (`ПакетXDTO`, `ВнешнийИсточникДанных`)
- **Предупреждение о версии индекса** — при загрузке индекса, собранного старой версией, rlm_start выдаёт warning с рекомендацией перестроить
- **Промпт для интеграционного анализа** — `docs/full_analysis_prompt.md` дополнен вторым промптом для E2E-теста интеграционных хелперов v1.4.0

### Исправлено
- **`find_print_forms` пропускал печатные формы ERP 2.x** — Pattern 2 (property-style: `КомандаПечати.Идентификатор`) не запускался если Pattern 1 (helper-style: `ДобавитьКомандуПечати()`) уже нашёл хотя бы одну форму. Теперь оба паттерна работают одновременно с дедупликацией по `name`. Результат на РеализацияТоваровУслуг: было 1, стало 13 печатных форм

### Изменено
- **`BUILDER_VERSION = 6`** — добавлены таблицы `http_services`, `web_services`, `xdto_packages` в SQLite-индекс
- **`get_statistics()` включает `builder_version`** — для проверки совместимости индекса
- **Шаги рецепта "интеграция" уточнены** — шаг 4 (планы обмена) теперь явно указывает получить имена через `find_by_type`, шаг 6 (рег.задания) содержит готовый Python-фильтр вместо абстрактного "filtered by"

### Важно
- **Требуется перестроение индекса** — для работы новых хелперов (`find_http_services`, `find_web_services`, `find_xdto_packages`) необходимо перестроить индекс: `rlm-bsl-index index build <путь>`. Без перестроения хелперы работают через fallback (XML-парсинг в реальном времени)

### Тесты
- 30 новых тестов: парсеры (18), рецепты (9), категории (4), version (2)
- Было: 441 (v1.3.5), стало: **471**
- E2E верификация: EDT с индексом (ЕРП 2.5), CF с индексом (ЕРП 2.5.14), CF без индекса (БГУ). Агенты Sonnet + Cursor — 0 регрессий

## [Unreleased]

### Исправлено
- **`index update` — миграция интеграционных таблиц** — при обновлении индекса, собранного до v1.4.0, таблицы `http_services`, `web_services`, `xdto_packages` и их индексы создаются автоматически (`CREATE TABLE IF NOT EXISTS`). Ранее `_insert_metadata_tables()` падала с `OperationalError` на старом индексе без этих таблиц
- **`find_xdto_packages()` fallback — защита от отсутствия `Package.xdto`** — для EDT-пакетов `.mdo` может существовать без рядом лежащего `Package.xdto`. Ранее `read_file_fn()` выбрасывал `FileNotFoundError`, теперь пакет возвращается с пустым `types`
- **`save_config()` / `install()` / `uninstall()` — консистентный путь конфига** — `save_config()` писал в хардкод `CONFIG_FILE`, игнорируя `RLM_CONFIG_FILE`. Windows install прошивал в реестр тот же хардкод, `uninstall()` удалял его. Теперь все пути используют `_config_path()`, который учитывает override через `RLM_CONFIG_FILE`
- **`find_callers_context()` — унификация `_meta`** — fallback возвращал `{total_files, scanned_files, has_more}`, indexed путь — `{total_callers, returned, offset, has_more}`. Теперь оба пути возвращают единый контракт `{total_callers, returned, offset, has_more}`
- **`find_roles()` fallback — поле `object` в результате** — indexed путь включал `"object"` в каждый role-item, fallback при группировке терял это поле. Добавлено `"object": object_name` в grouped dict
- **`index update` — пересчёт `detected_prefixes`** — при полном `build()` кастомные префиксы пересчитывались и записывались в `index_meta`. В `update()` этот шаг отсутствовал — после инкрементального обновления префиксы оставались устаревшими. Добавлен `_detect_prefixes()` + запись в `index_meta` в конце `update()`
- **Утечка MCP transport-сессий** — включён `stateless_http=True` в FastMCP. Ранее каждый HTTP-запрос без заголовка `Mcp-Session-Id` создавал transport-сессию, которая оставалась в памяти навсегда (клиенты не шлют DELETE при отключении). Накопление сессий приводило к `WinError 10055` (исчерпание сокетов Windows) и падению службы
- **Health check создавал MCP-сессии** — watchdog делал `POST /mcp` с JSON-RPC телом каждые 30 сек, что создавало лишнюю transport-сессию при каждой проверке. Добавлен лёгкий `GET /health` endpoint (`{"status": "ok"}`), watchdog и `reinstall-service.ps1` переведены на него
- **Предупреждение PowerShell при Invoke-WebRequest** — добавлен `-UseBasicParsing` в verify-шаге `reinstall-service.ps1`

### Добавлено
- **Лог режима при старте** — `transport=streamable-http stateless_http=True host=... port=...` в server.log для диагностики
- **Лог первого успешного health check** — watchdog пишет `Health check OK (url)` один раз после старта
- **Фильтр `GET /health` в uvicorn access log** — `_HealthLogFilter` подавляет шум от health check каждые 30 сек

### Документация
- Актуализирован подсчёт хелперов: 28 BSL + 8 I/O + 2 LLM = **38** (было указано 29)
- Добавлены `grep_read` и `search_methods` в docs/HELPERS.md
- Описание `index update` в INDEXING.md дополнено: schema upgrade и пересчёт `detected_prefixes`

### Тесты
- 5 новых тестов стабилизации: миграция integration tables, пересчёт prefixes, XDTO без Package.xdto, save_config override (2 теста)
- Обновлены 3 теста `find_callers_context`: `_meta` → новый контракт `{total_callers, returned, offset, has_more}`
- Расширен тест `find_roles`: проверка наличия поля `object` в каждом role-item
- E2E верификация: EDT+index (ЕРП 2.5), CF+index (ЕРП 2.5.14) — 0 регрессий, метрики совпадают с baseline v1.4.0
- Было: 471 (v1.4.0), стало: **476**

## [1.3.5] — 2026-03-23

### Добавлено
- **Бизнес-рецепты в стратегии** — `_BUSINESS_RECIPES` dict с 5 доменами (себестоимость, проведение, распределение, печать, права). Каждый домен содержит `compact` (3-4 шага) и `full` (6-7 шагов с альтернативами) план анализа
- **Step 0 — UNDERSTAND в WORKFLOW** — новый шаг перед DISCOVER: подсказка агенту декодировать бизнес-вопрос и найти рецепт
- **Динамическая инъекция рецепта в `get_strategy()`** — новый параметр `query`, матчинг по доменным ключевым словам через `_match_recipe()`. Уровень детализации: `compact` при low/medium, `full` при high/max. Без совпадения — только generic Step 0
- **Прокидывание `query` из `server.py`** — текст пользовательского запроса из `rlm_start` передаётся в `get_strategy()` для выбора релевантного рецепта
- **Логирование символов и токенов MCP-трафика** — `rlm_start` и `rlm_execute` пишут `out_chars` / `out_tokens~` в лог, `rlm_end` выдаёт итог сессии: `in_chars`, `out_chars`, `total_chars`, `total_tokens~` (оценка: chars / 1.75 для смешанного кириллица+код)

### Тесты
- 12 новых тестов бизнес-рецептов: структура dict, `_match_recipe()`, compact/full инъекция, case-insensitive, all domains
- Было: 429 (v1.3.4), стало: **441**

## [1.3.4] — 2026-03-22

### Добавлено
- **Indexed glob `Dir/**/*.ext`** — новая стратегия `prefix_recursive_ext` в `_can_index_glob()`. Паттерн `Subsystems/**/*.mdo` теперь мгновенный из SQLite вместо FS fallback (2.8s на медленном ПК)
- **Warmup тяжёлых модулей при старте сервиса** — `_warmup_imports()` в фоновом потоке: `bsl_helpers`, `bsl_xml_parsers`, `bsl_index`, `helpers`, `openai`. Запускается перед `mcp.run()`, снижает cold start первого `rlm_start`

### Изменено
- **Оптимизация `get_callers()` COUNT** — при отсутствии `module_hint` COUNT выполняется по одной таблице `calls` (использует `idx_calls_callee`) вместо дорогого COUNT через JOIN. При наличии `module_hint` — точный COUNT через JOIN (без изменений)

### Тесты
- 6 новых тестов `prefix_recursive_ext` (распознавание + SQL + parity)
- 5 новых тестов `get_callers` (meta без hint, meta с hint, pagination, zero callers, qualified calls)

## [1.3.3] — 2026-03-20

### Добавлено
- **Агрегация хелперов в логе** — `_format_helper_summary()` группирует повторяющиеся хелперы: `code_metrics(6×, total=0.7s)` вместо 6 отдельных записей. Порядок групп — по первому появлению (dict insertion order)
- **Логирование glob fallback с причинами** — `glob_files()` логирует причину FS fallback (`reason=no_index`, `reason=unsupported`, `reason=index_error`), indexed-hit на `logger.debug`. Диагностика для выявления медленных паттернов
- **`idx_zero_callers_authoritative`** — при fresh-индексе + has_calls пустой результат `find_callers_context()` считается окончательным (без 40s+ FS fallback). Возвращает `_meta.hint` с рекомендацией `safe_grep()`. При stale/нет индекса — fallback сохраняется
- **Warmup `openai` import** — `warmup_openai_import()` с lock+flag, запускается в фоновом потоке из `_rlm_start` только при `RLM_LLM_BASE_URL`. Параллельно с построением Sandbox (~13s на медленном ПК). Без side-effect на import-time
- **Паттерн `**/Dir/**/*.ext` в whitelist** — стратегия `under_prefix_ext` в `_can_index_glob()`. Покрывает EventSubscriptions, ScheduledJobs, FunctionalOptions (`**/EventSubscriptions/**/*.xml` и т.п.)
- **Версия в описании службы** — Windows и Linux службы включают номер версии в Description (`v1.3.3`)

### Изменено
- **`make_bsl_helpers()`** — новый параметр `idx_zero_callers_authoritative: bool = False`
- **`Sandbox.__init__()`** — пробрасывает `idx_zero_callers_authoritative` в `make_bsl_helpers()`
- **`_rlm_start()`** — вычисляет `_callers_authoritative` из `IndexStatus.FRESH + has_calls`, запускает openai warmup до Sandbox
- **`simple-install.ps1`** — добавлено обновление глобального Python (как в `reinstall-service.ps1`), вывод версии

### Ожидаемый эффект на медленном ПК (ERP, 12K BSL, с индексом)

| Bottleneck | v1.3.2 | v1.3.3 |
|------------|--------|--------|
| find_callers_context FS fallback (0 callers) | 49s (timeout) | ~0s |
| sandbox / openai import на HDD | 13s | ~5-8s |
| find_event_subscriptions (без индекса) | 11.5s | ~0s |
| find_scheduled_jobs (без индекса) | 11.9s | ~0s |
| find_functional_options (без индекса) | 20.1s | ~0s |
| find_roles (без индекса) | 23.8s | ~0.1s |

### Тесты
- Было: 398 (v1.3.2)
- Стало: 418 (добавлены 20 тестов: 4 format_helper_summary, 4 glob fallback logging, 4 authoritative callers, 3 warmup, 5 under_prefix_ext)

## [1.3.2] — 2026-03-20

### Добавлено
- **Таблица `file_paths` в SQLite-индексе** — навигационный индекс всех `.bsl`/`.mdo`/`.xml` файлов. `glob_files()`, `tree()`, `find_files()` мгновенно из индекса для поддерживаемых паттернов (было ~315с на медленном ПК, стало <1с)
- **Whitelist-диспетчер `_can_index_glob()`** — безопасная трансляция ограниченного набора glob-паттернов в SQL-запросы: `**/*.ext`, `Dir/**`, `Dir/*/File.ext`, точные пути, `**/Name.ext`. Всё остальное → fallback на FS
- **`IndexReader.glob_files()`** — поиск файлов по glob-паттерну из индекса
- **`IndexReader.tree_paths()`** — получение путей для tree-рендеринга из индекса
- **`IndexReader.find_files_indexed()`** — поиск файлов по подстроке с ранжированием: exact filename > prefix > substring filename > substring path
- **Ранжирование `find_files()`** — при использовании индекса результаты ранжируются по релевантности, а не только по алфавиту
- **Hints в стратегии для файловой навигации** — описание быстрых (indexed) и медленных (FS) паттернов, рекомендация `find_module()`/`find_by_type()` вместо `glob_files()` для BSL

### Изменено
- **`builder_version = 5`** — добавлена таблица `file_paths`, расширена `index_meta` (file_paths_count)
- **`make_helpers(base_path, idx_reader=None)`** — стандартные хелперы `glob_files`/`tree`/`find_files` теперь принимают `idx_reader` через замыкание (thread-safe, без глобального состояния)
- **`Sandbox._setup_namespace()`** — передаёт `idx_reader` в `make_helpers()` для ускорения файловой навигации
- **`index info` / `index build`** — показывают `FilePaths: N` в выводе
- **Стратегия `== INDEX ==`** — включает информацию о file_paths и tips по использованию индексированных паттернов
- **`RLM_EXECUTE_DESCRIPTION`** — добавлены `search_methods`, `extract_queries`, `code_metrics`, `find_files` в описание MCP-инструмента

### Исправлено
- **`find_files_indexed()` + Кириллица** — SQLite `LOWER()` работает только с ASCII; кириллические имена файлов не находились. Убран `LOWER()` из SQL, ранжирование перенесено в Python (`str.lower()` корректно обрабатывает Unicode)
- **Рецепты `find_register_movements` / `find_register_writers`** — использовали `r['lines']`, которого нет в indexed-пути (KeyError). Теперь `r.get('lines') or r.get('source', '')` — совместимы с обоими путями
- **`_STRATEGY_IO_SECTION`** — убрано дублирование FAST/SLOW glob-паттернов (оставлено только в условной секции INDEX TIPS). Уточнена формулировка `tree('.')`: "produces too much output" вместо "Avoid" (с индексом быстро, но объём вывода чрезмерен)
- **`find_roles()` — полное имя объекта в индексе** — builder делал `rsplit(".", 1)[-1]`, сохраняя `ТестСправочник` вместо `Catalog.ТестСправочник`. Wildcard-роли (напр. `кст_БазовыеПрава` с правами на `Document.*`) терялись. Теперь хранится полное имя, reader ищет через `LIKE`
- **`find_register_movements()` — паритет index vs FS** — три исправления: (1) `code_registers` включал все source, теперь фильтруется по `source='code'`; (2) `_MANAGER_TABLE_RE` ловил вызовы `ТекстЗапросаТаблицаXxx()`, теперь только определения `Функция|Процедура ТекстЗапросаТаблицаXxx()`; (3) добавлено извлечение `adapted`-регистров из `АдаптированныйТекстЗапросаДвиженийПоРегистру` в builder и helper
- **`get_register_movements()` — SELECT DISTINCT** — убраны дубли записей (напр. `РеестрДокументов` дважды в adapted-ветке)
- **`index update` — refresh role_rights** — при инкрементальном обновлении таблица `role_rights` не обновлялась. Также исправлен early return при отсутствии BSL-дельты: теперь metadata/role_rights/file_paths обновляются всегда

### Ожидаемый эффект на медленном ПК (ERP, 12K BSL)

| Хелпер | Было (v1.3.1) | Стало (v1.3.2) |
|--------|--------------|----------------|
| glob_files(`**/*.mdo`) | 88.6с (timeout) | <0.1с |
| glob_files(Documents/*) | 65.4с (timeout) | <0.1с |
| tree(.) | 45.0с (timeout) | <1с |
| find_files() | 49.5с (timeout) | <0.1с |
| **Суммарно FS-операции** | **~315с** | **<2с** |

### Тесты
- Было: 376 (v1.3.1)
- Стало: 398 (добавлены 49 тестов file_paths + 5 тестов index/FS parity + 1 тест update role_rights refresh)

## [1.3.1] — 2026-03-19

### Добавлено
- **Fast-path startup из index_meta** — при fresh-индексе `rlm_start` восстанавливает FormatInfo и ExtensionContext из метаданных индекса, пропуская `detect_format()` и `detect_extension_context()` (43с → <1с на медленном ПК)
- **Тайминги подэтапов в rlm_start** — логирование длительности каждого подэтапа (`format`, `ext`, `overrides`, `index`, `sandbox`, `prefixes`, `strategy`) + источник данных (`index`/`disk`/`fallback`)
- **Таблица `enum_values` в SQLite-индексе** — значения перечислений с синонимами. `find_enum_values()` мгновенно из индекса (было 120с на медленном ПК)
- **Таблица `subsystem_content` в SQLite-индексе** — нормализованное хранение состава подсистем. `analyze_subsystem()` мгновенно из индекса, поиск подсистем по имени объекта (обратный lookup). Удалён бесполезный glob-паттерн `**/*{name}*.mdo`
- **`IndexReader.get_startup_meta()`** — кэшированные метаданные для быстрого старта: `source_format`, `shallow_bsl_count`, `config_role`, `extension_prefix`, `extension_purpose`
- **`IndexReader.get_enum_values()`** — поиск перечислений по имени через SQLite
- **`IndexReader.get_subsystems_for_object()`** — обратный поиск подсистем по имени объекта
- **Диагностика `find_callers_context`** — debug-логирование source (index/fallback), тайминг count_query и rows_query в `get_callers()`

### Изменено
- **`builder_version = 4`** — добавлены таблицы `enum_values`, `subsystem_content`; расширена `index_meta` (shallow_bsl_count, extension_prefix, extension_purpose, has_configuration_xml)
- **`_parse_configuration_meta()`** — дополнительно сохраняет `shallow_bsl_count`, `extension_prefix`, `extension_purpose`, `has_configuration_xml` в index_meta
- **`_rlm_start()` реструктурирован** — индекс загружается первым, затем fast path (из meta) или disk path (полное сканирование). Drift check только при disk path

### Исправлено
- **`find_enum_values()` — fallback при промахе** — если таблица `enum_values` есть, но enum не найден, возвращает ошибку мгновенно (ранее fallback на glob 11с)
- **`parse_rights_xml()` — поддержка namespace 8.2 и 8.3** — автоопределение namespace из root tag, поддержка обеих версий `http://v8.1c.ru/8.2/roles` и `http://v8.1c.ru/8.3/roles`
- **Builder ролей — переход на ElementTree** — `_parse_role_rights_for_index()` использует `parse_rights_xml()` вместо regex-парсера, который пропускал >97% записей из-за несовпадения namespace
- **`find_roles()` — дедупликация по роли** — fallback-путь группирует результаты по `role_name` и объединяет права (было 117 записей вместо 4 уникальных ролей)
- **Drift warning — корректное сравнение** — shallow vs shallow (`shallow_bsl_count` из index_meta) вместо shallow vs full. При fast path drift check пропускается

### E2E результаты (ERP, 486K методов, этот же ПК)

| Хелпер | v1.3.0 без индекса | v1.3.1 с индексом v4 |
|--------|-------------------|---------------------|
| rlm_start | 15.5с | **0.54с** |
| analyze_document_flow | 24.4с | **0.3с** |
| find_roles | 25.2с | **0.0с** |
| find_functional_options | 21.2с | **0.0с** |
| find_enum_values (hit) | 11.7с | **0.0с** |
| find_enum_values (miss) | 11.6с | **0.0с** |
| analyze_subsystem | 10.6с | **0.0с** |
| **Полная сессия (3 calls)** | ~120с | **~2.8с** |

## [1.3.0] — 2026-03-19

### Добавлено
- **Таблица `role_rights` в SQLite-индексе** — нормализованное хранение прав ролей. Regex-парсинг `Rights.xml` (CF) и `*.rights` (EDT). Параллельная индексация через `ThreadPoolExecutor` совместно с BSL-модулями.
- **Таблица `register_movements` в SQLite-индексе** — движения документов по регистрам из трёх источников: `erp_mechanism` (МеханизмыДокумента), `manager_table` (ТекстЗапросаТаблицаXxx), `code` (Движения.Xxx). `NamedTuple` для in-band данных.
- **`detected_prefixes` в `index_meta`** — при сборке индекса определяются кастомные префиксы расширений, сохраняются в метаданные. `IndexReader` возвращает их в `get_statistics()`, `rlm_start` подхватывает из индекса.
- **`find_roles(obj_name)` — мгновенный хелпер** — поиск ролей объекта из таблицы `role_rights` с автоматическим fallback на XML-сканирование.
- **`find_register_movements(doc_name)` — мгновенный хелпер** — движения документа из таблицы `register_movements` + fallback на code-парсинг.
- **`find_register_writers(reg_name)` — мгновенный хелпер** — документы, пишущие в указанный регистр, из таблицы `register_movements`.
- **Freshness check (usable/strict)** — двухуровневая проверка свежести индекса: quick (возраст + семплирование) и strict (полный пересчёт). `_index_state` инициализируется из SQLite-таблицы `modules` вместо glob.
- **`code_metrics` single-pass** — метрики BSL-модуля вычисляются за один проход по строкам (ранее — множественные regex).
- **`safe_grep` параллельный** — `ThreadPoolExecutor` с сортировкой результатов по `(file, line)`.
- **Streamable HTTP: обработка 421 Misdirected Request** — корректный ответ вместо падения при попытке SSE-подключения к Streamable HTTP эндпоинту.

### Изменено
- **`builder_version = 3`** — новая версия формата индекса (добавлены таблицы `role_rights`, `register_movements`, поле `detected_prefixes` в `index_meta`).
- **Стратегия** — секции `== INDEX ==` и `== HELPERS ==` обновлены: `find_roles`, `find_register_movements`, `find_register_writers` указаны как INSTANT при наличии индекса.
- **`reinstall-service.ps1`** — добавлена проверка наличия `pip`; обновление глобального Python через `uv pip install` перед `uv tool install`.

### Исправлено
- **`find_custom_modifications`** — EDT resolve `.mdo` файлов, порог эвристики для определения префиксов расширений.
- **MCP SDK client timeout** — задокументировано ограничение (клиент не передаёт таймаут, используется серверный `execution_timeout_seconds`).

### Тесты
- Было: 320 (v1.2.0)
- Стало: 343 (добавлены тесты `role_rights`, `register_movements`, freshness check, single-pass metrics, parallel grep, 421 handler)

## [1.2.0] — 2026-03-18

### Добавлено
- **Прозрачное ускорение хелперов через SQLite-индекс (Этап 2)** — при наличии индекса `extract_procedures`, `find_exports`, `find_callers_context`, `find_event_subscriptions`, `find_scheduled_jobs`, `find_functional_options` работают мгновенно из SQLite с автоматическим fallback на live-парсинг.
- **Новый хелпер `search_methods(query, limit=30)`** — полнотекстовый FTS5-поиск методов по подстроке имени с BM25-ранжированием. Работает только при наличии индекса с FTS.
- **Секция `== INDEX ==` в стратегии** — при загрузке индекса `rlm_start` добавляет информацию о количестве методов/вызовов, доступных мгновенных хелперах и подсказки по оптимальному батчингу.
- **Поле `index` в ответе `rlm_start`** — JSON с loaded, methods, calls, has_fts, config_name, config_version, warnings.
- **Авто-резолв XML-путей** — `parse_object_xml('Documents/Name')` автоматически находит XML (ранее требовался полный путь `Documents/Name/Ext/Document.xml`).
- **Error hints в песочнице** — при ошибках `FileNotFoundError`, `TimeoutError`, `NameError` в sandbox добавляются подсказки HINT с рекомендацией.
- **Предупреждения о медленных хелперах** — `analyze_document_flow` и `analyze_object` помечены CAUTION в стратегии для больших конфигураций.
- **Индекс методов BSL (SQLite) — Этапы 1+1.1** — автономный модуль `bsl_index.py` + CLI `rlm-bsl-index` (команды `build`, `update`, `info`, `drop`). 7 таблиц: `modules`, `methods`, `calls`, `index_meta`, `event_subscriptions`, `scheduled_jobs`, `functional_options`. FTS5 полнотекстовый поиск.
- **Метаданные конфигурации в индексе** — при build парсится `Configuration.xml` / `.mdo`: имя, версия, поставщик, формат (CF/EDT), роль (base/extension). Флаг `--no-metadata` для пропуска Level-2 таблиц (ES/SJ/FO).
- **Единая загрузка `.env`** — модуль `_config.py` с `load_project_env()`. CLI и MCP-сервер используют одну цепочку поиска: `service.json` → user-level `.env` → CWD.

### Изменено
- `get_statistics()` в `IndexReader` — boolean-флаги `has_fts`/`has_metadata` теперь возвращаются как `bool` вместо строк `"1"`/`"0"`.
- `get_scheduled_jobs()` в `IndexReader` — фильтрация по имени выполняется через SQL `WHERE name LIKE ?` вместо Python.
- `_resolve_object_xml()` — проверка существования файла через `resolve_safe().exists()` вместо чтения файла целиком.
- `find_custom_modifications()` и `analyze_object()` — упрощены: вместо перебора путей (`for xp in [...]`) используют `parse_object_xml` с авто-резолвом.
- `json` импорт в `bsl_index.py` вынесен на уровень модуля (убраны 3 inline `import json as _json`).
- Убрана двойная сортировка путей при вычислении `paths_hash`.

### Исправлено
- Парсинг inline-комментариев в `.env` файле Windows-службой (`_load_env_file`) — `RLM_INDEX_MAX_AGE_DAYS=7 # comment` корректно парсится как `7`.
- CI: обработка `PermissionError` в `extension_detector` на Linux snap.
- CI: добавлен `pythonpath` для тестов на Linux.
- CI: `dependency-groups.dev` для совместимости `uv sync --dev`.

### Тесты
- Было: 290 (v1.1.0 + Этапы 1/1.1)
- Стало: 320 (добавлены тесты интеграции индекса: 30 тестов в `test_bsl_index_integration.py`)

## [1.1.0] — 2026-03-16

### Добавлено
- **Реестр хелперов** — `_registry` + `_reg()` внутри `make_bsl_helpers()`. Strategy text, help-рецепты и available_functions генерируются автоматически из реестра. Добавление нового хелпера = функция + `_reg()`.
- **Новый хелпер `extract_queries(path)`** — извлечение встроенных запросов 1С из BSL-модулей. Парсит `Запрос.Текст = "..."` и многострочные `|`-тексты, определяет таблицы и процедуру-владельца.
- **Новый хелпер `code_metrics(path)`** — метрики BSL-модуля: строки кода/комментариев/пустые, число процедур, средний размер, максимальная вложенность.
- **GitHub Actions CI** — автоматический прогон тестов на push/PR (Ubuntu + Windows, Python 3.10 + 3.12).
- **`LazyList` / `LazyDict`** — утилиты для thread-safe lazy init с double-check locking. Заменяют 5 копипаст boilerplate в кэшах хелперов.

### Изменено
- **XML-парсеры** вынесены в отдельный модуль `bsl_xml_parsers.py` (~770 строк). `bsl_helpers.py` стал компактнее.
- **Тестовая инфраструктура** — `tests/conftest.py` с pytest-фикстурой `bsl_env`. Базовые тесты упрощены.
- **PyPI metadata** — добавлены `authors`, `classifiers`, `keywords` в `pyproject.toml`.

### Количество тестов
- Было: 234
- Стало: 236+ (добавлены тесты для extract_queries и code_metrics)

## [1.0.0] — 2026-03-13

Первая публичная версия.

- 27 BSL-хелперов песочницы + 8 стандартных (read_file, grep, glob и т.д.)
- Поддержка CF и EDT/MDO форматов
- Дисковый кэш индекса BSL-файлов
- XML-парсеры метаданных 1С (6 типов)
- Авто-детект нетиповых префиксов из кодовой базы
- Auto-strip типов метаданных (Документ.X → X)
- Параллельный prefilter (ThreadPoolExecutor) для find_callers_context
- Авто-детект расширений 1С (extension_detector)
- OpenAI-совместимый llm_query + Anthropic fallback
- Windows/Linux системная служба
- StreamableHTTP транспорт
- Thread-safety для параллельных MCP-сессий
- 234 теста
