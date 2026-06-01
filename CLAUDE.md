# levelup-back

Django multi-tenant (django-tenants) SaaS бэкенд для системы лояльности ресторанов, обслуживающий VK mini-app. Один Postgres, один Redis, Celery (worker + beat), Gunicorn — всё в docker-compose.

## Раскладка кода

- `main/` — Django project root: `settings.py`, `urls.py` (тенант), `public_urls.py` (public schema), `celery.py`.
- `apps/shared/` — модели в public schema:
  - `clients/` — `Company` (тенант), `Domain`, биллинг.
  - `guest/` — `Client` (VK-пользователь, привязка по `vk_id`).
  - `users/` — кастомная `User` модель с ролями (`superadmin` / `network_admin` / `client`).
  - `config/admin_sites.py` — `PublicAdminSite` (`/superadmin/`) и `TenantAdminSite` (`/admin/`).
- `apps/tenant/` — модели per-schema. Ключевые: `branch` (`Branch`, `ClientBranch`, `CoinTransaction`, `Cooldown`, `DailyCode`, `TestimonialConversation`), `delivery`, `game`, `quest`, `inventory`, `catalog`, `analytics` (RFM-сегменты + AI-анализ отзывов), `senler`, `telegram`.
- Шаблоны кастомной админки: `templates/admin/`.

## Многотенантность

`django-tenants`. Public schema хранит `Company` + `Domain`; каждая компания живёт в своей Postgres-схеме. Маршрутизация по host — `<schema>.levelupapp.ru`. `TenantMainMiddleware` ставит `request.tenant`. URL-confs:
- public schema → `main/public_urls.py`
- любой тенант → `main/urls.py` (`ROOT_URLCONF`)

`SHARED_APPS` + `TENANT_APPS` в `settings.py` определяют, какие модели где живут. Миграции применяются Django через `TenantSyncRouter`.

## Доставка (Delivery)

- POS-система (Dooglys / iiko) шлёт webhook на `https://levelupapp.ru/api/v1/delivery/webhook/` (public) — `PublicDeliveryWebhook` ищет тенанта по `dooglys_branch_id` / `iiko_organization_id` и сохраняет `Delivery` в нужной схеме. Останавливается на **первом совпадении** — следить за коллизиями id между тенантами.
- Гость в мини-приложении вводит последние 5 цифр кода → `POST /api/v1/code/` (tenant scope) → `DeliveryCodeView` → `activate_delivery(short_code, vk_id, branch_id)`.
- Реально работает только в `asap_bryansk` и `asap_orel` (Dooglys), `levone` (iiko). У остальных тенантов POS-id в `Branch` не заполнены → `Delivery` всегда пустая → активация = 404.

## Архитектурные паттерны (вводились в бэклоге LU-01..14, 2026-05-28)

Эти паттерны переиспользуются в новых задачах — не изобретать заново.

### 1. Per-tenant + per-branch override настроек

Поле живёт на двух уровнях:

- `ClientConfig.<field>` (public schema, `apps/shared/config/models.py`) — общее для всей сети, с осмысленным дефолтом.
- `BranchConfig.<field>` (tenant schema, `apps/tenant/branch/models.py`) — opt-in override для конкретной точки. Пустое значение (`''` для TextField или `0` для number) трактуется как «использовать значение тенанта».

Резолв в `get_branch_info(...)` в `apps/tenant/branch/api/services.py`: `branch override → tenant default → hardcoded fallback`. Возвращается готовая строка в `BranchInfoSerializer` — фронт логику резолва не знает.

**Примеры:** `code_prompt_message`, `quest_show_message` (LU-02), `birthday_window_days` (LU-13).

### 2. M2M «объект → много точек» по образцу `Product`

Шаблон для квестов, акций, заданий — когда объект сети должен работать на нескольких точках.

- Through-таблица (`ProductBranch`, `QuestBranch`) с полями `ordering`, `is_active`, `unique_together (object, branch)`.
- На главной модели: `branches = ManyToManyField('branch.Branch', through='XBranch', related_name='quests', blank=True)`.
- Админка: `TabularInline` для through-таблицы — выбираешь точки внизу формы редактирования.
- **Безопасный переход с FK на M2M:** старый FK не удалять сразу. Сделать `null=True, blank=True`, переименовать `related_name` (например, `quests_legacy`), оставить как поле legacy. Это даёт откат через git revert + migrate reverse без потери данных.
- API: принимать `branch_ids: [..]`, `all_branches: true`, или legacy `branch_id`.

**Пример:** `Quest.branches` (LU-09).

### 3. Скрытие вместо удаления

`Product.is_archived` (Boolean, indexed). Во всех queryset'ах выдачи добавляется `.filter(is_archived=False)`. Уже выданные объекты в инвентаре гостей остаются доступными к активации — данные не теряются, аналитика остаётся целой.

В админке: `list_filter = ('is_archived', ...)` + bulk-actions «🗄️ Архивировать» / «♻️ Восстановить» + badge в `flags_badges`.

**Пример:** `Product.is_archived` (LU-10). При добавлении нового «удаляемого» типа — лучше архивировать.

### 4. Re-sync внешнего статуса с Redis-кешем

`_resync_vk_status_cached(profile)` в `apps/tenant/branch/api/services.py` дёргает VK API на каждом init гостя для пересинхронизации `is_community_member` / `is_newsletter_subscriber`. Cache key `vk_sub_resync:{branch_id}:{vk_id}` TTL **60 секунд** — не превышает VK rate limit (≤20/sec). При ошибке одного метода **не перезаписывает** соответствующее поле (берёт текущее из БД).

**Пример:** LU-05. Тот же паттерн применим к любой sync со сторонним API.

### 5. AI pre-classifier до Anthropic API

`_try_numeric_rating(text, source)` в `apps/tenant/analytics/ai_service.py` срабатывает **до** вызова Claude. Распознаёт «только цифра» (с эмодзи/пунктуацией) и раскладывает по шкале по правилам, без обращения к AI. Экономит API quota + не зависит от KB.

**Пример:** LU-04. Паттерн применим к любым «правилам поверх AI» (детекция шаблонных сообщений, чёрный список, и т.д.).

### 6. VK message poll — критичные нюансы

- `messages.getConversations(filter='all', count=50)` — НЕ `unread`, иначе пропускаем conv где менеджер ответил, а гость молчит.
- Глобальный дедуп по `vk_message_id` (поверх всех conv) — иначе при overlap'е `messages.getHistory` сохраняется дубль.
- Для исторических сообщений сохранять `created_at = datetime.fromtimestamp(msg['date'], tz=timezone.utc)` — НЕ забыть `tz=utc`, иначе 3-часовой сдвиг.
- `is_replied=True` ставить **только если** наш admin reply — последний по `vk_message_id` в треде. Сравнивать `int(vk_message_id)` с гостевыми.
- Lookup conv по `vk_sender_id`: использовать `.filter(...).order_by('-last_message_at').first()` — НЕ `.get()`, у старых гостей могут быть legacy conv с `branch=X` (см. Fix A) + новый с `branch=None`, что даст `MultipleObjectsReturned`.

**Пример:** LU-08 (6 фиксов).

### 7. Скрытие секций отчёта через query-param

`?hide=4,6,7` и `?hide_widgets=4_delivery,...` — каждая секция и под-виджет в шаблонах обёрнуты `{% if N not in hidden_sections %}` / `{% if 'X' not in hidden_widgets %}`. PDF автоматически уважает фильтр — `downloadPDF()` копирует `URLSearchParams(window.location.search)`.

**Пример:** LU-11. UI: чекбокс-форма наверху → JS обновляет URL → страница перезагружается.

### 8. VK API edit/delete отправленных сообщений (24ч окно)

`messages.edit(peer_id, message_id, message, group_id, access_token)` — редактирование исходящих сообщений сообщества **только в первые 24 часа** с момента отправки. После — VK вернёт ошибку (пропустить с warning, не падать).

`messages.delete(message_ids, group_id, delete_for_all=1)` — удаление пакетом (до 100 id за раз). После — пометить `BroadcastRecipient.status = SKIPPED` локально, чтобы метрики обновились.

**Пример:** `edit_broadcast_send_in_vk`, `delete_broadcast_send_in_vk` в `apps/tenant/senler/services.py` (LU-12).

### 9. Theming vk-miniapp через SCSS-переменные на CSS-var

`apps/tenant/branch/api/services.py:get_branch_info` отдаёт `brand_color` (HEX из `ClientConfig`). Фронт vk-miniapp в `useInitData` вызывает `applyBrandColor(hex)` (`src/helpers/applyBrandColor.js`), которая через `document.documentElement.style.setProperty('--main-purple', ...)` устанавливает 7 CSS-vars (генерация оттенков из одного HEX через `shadeHex(...)`).

В `src/styles/_variables.scss` все 7 фиолетовых SCSS-переменных переопределены через `var(--name)`. В `:root` дефолтные HEX как fallback.

**Важно:** не использовать SCSS color-функции (`lighten`, `darken`) на этих переменных — `var()` не работает с compile-time функциями.

**Пример:** LU-14.

## Известные подводные камни

- **Django 5.1 + `format_html`**: вызов без аргументов (`format_html('<span>...</span>')`) теперь кидает `TypeError`. В админках уже починено (заменено на `mark_safe`). При добавлении новых badge-методов в `@admin.display` — либо ставь `{}` и передавай значения, либо используй `mark_safe` для статической разметки.
- **`except Exception` в DRF-views**: обязательно после конкретных `except ClientNotFound` / `except DeliveryNotFound`, иначе catch-all поглощает их и легитимные 404 становятся 500.
- **Логирование на проде**: в `settings.py` нет `LOGGING`, gunicorn без `--access-logfile` → 500-ошибки не попадают в `docker logs web`. Для диагностики либо хот-патчи `logger.exception` в подозрительный view, либо ходи на эндпоинт с `Invoke-WebRequest` — `DEBUG=True`, traceback в теле ответа.
- **Anthropic API credits**: исчерпаны, `ai_service.py` забивает stderr `BadRequestError 400` — фильтруй (`grep -v "ai_service\|anthropic"`), не путай с настоящими багами.
- **VK-отзывы: НЕ удалять диалоги ради «чистки», только нейтрализовать (LU-42)**. `poll` ресинкается с VK (`getConversations`) — удалённые треды возвращаются (особенно после рассылки: она делает всех гостей «активными» → poll видит их как новых и пересоздаёт). Чинить флаги (`is_replied`/`has_unread`/`last_message_at`) — старое само уходит вниз по реальной дате. Команды: `repair_vk_incident --no-delete --commit` (нейтрализация выкопанной истории + отсев рассылок-как-ADMIN_REPLY), `dedup_vk_convs --commit` (мердж дублей `(branch,vk_sender_id)` от гонок poll+Callback). **НИКОГДА** не гонять `reconcile_all_vk_messages_task` «на всю историю» и не вешать в beat: `TestimonialConversation.sentiment` default=`WAITING` + `reclassify_waiting_reviews_task` (теперь с recency-guard) = риск флуда синхронных пушей за старьё. Исторический импорт обязан быть тихим и `has_unread=False`.

## Локальная разработка

```bash
docker compose up -d
docker exec -it web python manage.py shell
```

Конфиг — `.env/.env.dev` (есть `.env.dev` на проде с `DEBUG=True`, `POSTGRES_*`, `ANTHROPIC_API_KEY`, `DELIVERY_WEBHOOK_SECRET`, `VK_SECRET`, и т.д.).

Миграции для tenant-схем:
```bash
docker exec web python manage.py migrate_schemas --shared
docker exec web python manage.py migrate_schemas
```

## Прод-операции

- Сервер: `root@81.17.154.208` (Ubuntu 24.04, Docker compose). Код в `~levone/levelup-back`.
- Контейнеры: `web`, `celery-worker`, `celery-beat`, `database` (postgres:16-alpine), `redis`, `checkup_redis`.
- Хот-патч без билда: правишь файл внутри контейнера через `docker exec -u 0 web python3 ...` и потом `docker kill -s HUP web` — gunicorn graceful-перезапустит воркеров. После хот-патча **обязательно** синхронизируй с локальным репозиторием, иначе `docker compose build` затрёт.

### Деплой: что чем перезагружать (КРИТИЧНО — не «всё через HUP web»)

Стандартный деплой: `git push` → на сервере `cd /home/levone/levelup-back && sudo -u levone git pull --ff-only origin main` → перезагрузка по типу правки:

| Что менялось | Команда перезагрузки |
|---|---|
| `.py` во вью/web | `docker kill -s HUP web` (graceful reload gunicorn) |
| celery-задача (senler/relay/push tasks) | `docker restart celery-worker celery-beat` — **HUP НЕ перезагружает celery** |
| **новый** `@shared_task` | `docker restart celery-worker` — регистрируется через autodiscover только при старте воркера |
| `static/**` (JS/CSS) | `docker exec web python manage.py collectstatic --noinput` — nginx раздаёт `/static/` из `staticfiles/`, `git pull` обновляет только исходный `static/`. Файлы не хэшируются → пользователю hard-refresh (Ctrl+F5) |
| `.env` / `compose.yaml` | `docker compose up -d --force-recreate web` (env_file читается только при create; HUP не перечитывает) |

Все шаги идемпотентны (pull ff-only, HUP, restart, collectstatic) — безопасно повторять после обрыва SSH. Перед `git pull` проверять `git status --porcelain` сервера (чисто ли — иначе риск как 14.05).

### VK rate limit (бан-риск)

≤ **20 messages/sec** на `vk_community_token`, иначе VK банит сообщество. `run_broadcast` держит лимит через `time.sleep(0.05)`. celery-worker запущен `--concurrency=2` → **нельзя** дробить рассылку на несколько параллельных celery-тасков на один токен (суммарно >20/с). Паттерн: один **серийный** таск на запрос — `apps.tenant.senler.tasks.run_broadcast_task(schema_name, send_ids)`. Все SenlerConfig одного тенанта делят один `vk_group_id`/токен.

## Регламент коммитов (отче наш)

**ОБЯЗАТЕЛЬНО**: после **любого** изменения файлов на проде или в репозитории — сразу `git add → git commit → git push origin main`. Без исключений.

**Почему так**: 2026-05-14 случилась катастрофа — overlay контейнера хранил несколько недель работы (фиолетовая админка, модуль `leads`, push-токены, audit log, mobile API, RF auto-reply, support chat), и эти изменения **никогда не попадали в git**. `docker compose up --force-recreate` уничтожил overlay → всё пропало. Восстановили только потому что у пользователя случайно сохранился снапшот 14:12 на другом компе. **Без этого снапшота — потеря недель работы.**

**Workflow для любых правок (хот-патч, новый код, миграция, фикс)**:
```bash
# 1. Изменения уже на хосте (через rsync/SFTP/прямой edit)
# 2. Сразу коммит:
cd /home/levone/levelup-back
sudo -u levone git add -A
sudo -u levone git commit -m "feat/fix: краткое описание"
sudo -u levone git push origin main
```

**Что включать в коммит**:
- Все `.py`, `.html`, `.css`, `.js` файлы
- Миграции (`apps/*/migrations/*.py`) — `.gitignore` исправлен 2026-05-14
- `requirements.txt`, `compose.yaml`, `Dockerfile`, `main/settings.py` если меняются

**Что НЕ включать** (уже в `.gitignore`):
- `*.bak`, `*.bak.*`, `backups/`, `backup_*.sql`, `host-pre-rsync-*.tar.gz`
- `staticfiles/`, `media/`, `redis_data/`, `postgres_data/`
- `__pycache__/`, `*.pyc`
- `.env`, `.env/`

**После хот-патча в контейнере** (если использовался `docker cp` или `docker exec`):
1. Синк изменений с `/home/levone/levelup-back/` (если bind-mount активен — уже синхронно)
2. Сразу коммит + пуш

**Откат**: `git log --oneline` → `git revert <hash>` или `git reset --hard <hash>` (последнее с осторожностью; перед reset делать `git stash` или `git branch backup-$(date +%s)`).