# Записанные ответы песочницы `dev` — волна 2, готовые ручки (для контрактных тестов CheckUp)

Записано 18.09.2026 на сети `dev` под токенами обмена (`fx-admin` = `network_admin`, `fx-client` = `client` точки `990002`,
внутренний `Branch.id` = 3; первая точка dev — `Branch.id` = 2). Формат тот же, что в `../w1/`: `request` (метод, путь,
заголовки с плейсхолдерами, тело) и `response` (статус, тело). Всё записано в одной транзакции с откатом: созданные
категории/товары/квесты/акции/коды дня в базе не остались. Скрипт: `record_fixtures_w2.py` (та же схема, что у w1).

Это **форма** ответов, не значения. Ручки существовали до платформы (их использует мобильное приложение LoyalUP),
поэтому у них свои шероховатости — см. контракт, раздел 3.2:
- ошибки валидации — `{"error": "…"}` (не `{code, detail}` как у `broadcasts/*` и не `{"detail"}` как у `mobile/reviews/*`);
- 404 по неизвестному `id` у PATCH/DELETE — стандартный DRF по-английски (`{"detail": "No Product matches the given query."}`);
- `DELETE` отвечает `204` без тела;
- `branch_id` / `branch_ids` везде **внутренние** `Branch.id`;
- RBAC по точкам есть у кодов дня, дней рождения и вовлечённости (`*_client_scoped`); каталог, квесты и акции отдают всё сети
  (ограничение по точкам для роли `client` — вопрос к волне 2, см. контракт).

## Состав (45 записей)

| Возможность | Файлы |
|---|---|
| №18 Коды дня | `daily_codes_list`, `daily_codes_list_client_scoped`, `daily_codes_generate_200` (`{branch_id, purpose: BIRTHDAY\|SUPERPRIZE\|…}`), `daily_codes_generate_400_no_branch`, `daily_codes_generate_400_bad_purpose`. Значения `code` **заменены синтетическими** — живые коды дня выдают подарки. |
| №20 Категории каталога | `catalog_categories_list`, `_list_filtered` (`?branch_ids=`), `_create_201` (`{branch_id, name, ordering}`), `_create_400_no_branch`, `_create_404_branch`, `_patch_200`, `_patch_400_bad_ordering`, `_patch_404`, `_delete_204` |
| №19 Каталог подарков | `catalog_products_list`, `_create_201` (JSON без картинки: `{name, price, description, emoji, is_super_prize, is_birthday_prize, assignments: [{branch_id, category_id, ordering, is_visible}]}`; картинка — тот же URL multipart, поле `image`), `_create_400_no_name`, `_create_400_bad_price`, `_patch_200`, `_patch_404`, `_delete_204` |
| №22 Квесты | `quests_list`, `_create_201` (`{name, description, reward, branch_ids \| all_branches, is_active, ordering}`), `_create_400_no_branches`, `_create_400_bad_reward`, `_create_404_branch`, `_patch_200`, `_patch_404`, `_delete_204` |
| №21 Акции и баннеры | `promotions_list`, `_create_201` (`{branch_id, title, discount, dates}`; картинка multipart `image`), `_create_400_missing`, `_create_404_branch`, `_patch_200`, `_patch_404`, `_delete_204` |
| №24 Аналитика вовлечённости | `analytics_engagement` (`?period_days=30`), `_branch` (`&branch_id=` внутренний), `_client_scoped` |
| №25 Дни рождения | `guests_birthdays` (`?days_ahead=365&include_past=1`), `_client_scoped`. Гости **обезличены** (VK ID → `1000000NN`, имена → «Гость Тестовый», телефон пуст). |
| №30 Лояльчик | `assistant_ask_200` (`{question, history?}` → `{answer, actions[]}`; вызов Claude при записи **заглушен** — текст синтетический, форма живая; живой вызов тратит кредиты API), `assistant_ask_400_empty`, `assistant_context` (`{greeting, suggestions[], stats{}}`) |
| №32 Связь с менеджером | `support_chat_manager` (`{id, name, role, avatar_url, online, last_seen, phone, work_hours, messages[], unread_count, last_message_at, last_message_text, last_message_sender}`) |

## Дополнение 19.09.2026 — новые ручки волны 2 (103 записи)

Записаны на `dev` в одной транзакции с откатом под теми же токенами обмена (`fx-admin` = `network_admin`, `fx-client` = `client` точки 990002,
внутренний `Branch.id` = 3; первая точка — id 2). Отправка в ВК, вызов Claude и celery-задача маркетолога **заглушены**; ключи кэша
(троттл тест-отправки, блокировка генерации) очищены; загруженный файл базы знаний удалён. Скрипт: `record_fixtures_w2b.py`.
Гостей в этих записях нет (`anonymized guests: 0`).

| Модуль | Префикс | Что записано |
|---|---|---|
| №26/27 точки контакта и материалы | `cp_*`, `branch_materials*` | create 201 (cafe, review со столом, другая точка) / 400 `table_required` / 400 `src` в теле / 404 чужая точка под client; list (+фильтры, client, 400 `period=30` — период это пресет `7d\|30d\|…`); detail; guests по стадии + 400; patch 200 (имя, режим до сканов) / 409 `has_scans` после синтетического скана; delete 204 / 409 `has_scans` / 404 после удаления; batch-tables 201 / 400; materials 200 / 404 под client |
| №23 сториз | `story_*` | сеть: get, patch 200, 400 чужое поле, 403 client; точка: get (admin и client), patch `false` перебивает сеть, patch `0`/`""` → `null`/`source: network`, 404 чужая точка под client |
| №28 отчёт | `report_*` | sections; comments get (пусто) / put 200 / 400 плохая секция / 409 `conflict` по `updated_at` / удаление пустым текстом / get с комментариями / 400 `period=30`; `comments/generate/` с `save` (Claude заглушен); `report/` с `comments`; `print/` — сводка HTML (`_content_type`, `_length`, `_has_script_tag: false`, `_head`) |
| №29 маркетолог | `marketer_*` | settings get / patch 400 `vk_wall_token` / 403 client / patch 200; generate 409 `marketer_disabled` → 202 (задача заглушена) → 409 `already_generating`; посты list / 400 `post_type` / detail / context / patch 200 / publish 400 `confirm_required` / 409 `no_vk_token` / reject 200 и `already_rejected` / patch 409 `not_editable` / 404 |
| №31 авторассылки | `ab_*` | events; list (+client_scoped: сетевые правила скрыты); create 201 / 400 `event_unknown` / `delay_required` / client без точек; detail; patch 200 / 400 пустой текст / 400 `use_activate`; preview (с `count`, `by_branch`, `sample_texts`; `explanation` — «кто получит и почему» словами, записано 19.09 поверх волны 3); activate 400 `expected_count_required` / `confirm_required` / `audience_empty` (песочница пуста) / 409 `audience_changed`; deactivate; log; stats; variants 201 / 400 `variant_weights_invalid` / patch / delete 204; test-send 400 `guest_not_found` (в песочнице нет гостя; успех и `rate_limited` требуют живого гостя); delete 204 = архив → patch 409 `archived`, list `include_archived=1`; 404 |
| №52 база знаний | `kb_*` | list; create 201 (multipart `.txt`) / 400 `.pdf` / 403 client; text; patch; delete 204 = архив; list `include_archived=1`; 404 |
| №56 флаги механик | `settings_features*` | get (admin и client) — только чтение |

Шероховатость записи: `cp_detail_200_client_own_point` — client точки 990002 видит QR своей точки (200); чужая точка — `cp_detail_404_foreign`.

## Дополнение 19.09.2026 — добивка карты v1.8: №49, №55, №56, №34 (46 записей)

Записаны 19.09 00:18 МСК на `dev` сразу после выкладки пачки `c404620`, в одной транзакции с откатом, под теми же токенами обмена
(`fx-admin` = `network_admin`, `fx-client` = `client` точки 990002 = `Branch.id` 3; первая точка — id 2) и под токеном с признаком
платформы (`platform: true` в теле обмена, контракт 3в) для `overview/*`. Вызовы ВК и celery **заглушены**. Скрипт: `record_fixtures_w2d.py`.
Гостей в записях нет. Секреты не возвращаются никогда — только `token_set` / `token_last4` / `secret_set`; в записях токены
синтетические (`vk1.a.fixture-token-*`, `fixture-secret`), хвост живого токена первой точки dev заменён на `x9Qz`.

| Модуль | Префикс | Что записано |
|---|---|---|
| №49 каталог наград (запись) | `rc_*` | list (ключ `items` сохранён + `total/limit/offset/tiers`), `list_catalog_mode` (`?include_inactive=1&include_archived=1&limit=`), `list_include_archived`; create 201 (`{product_id, tier, name, weight, default_lifetime_days, activation_limit, branch_id…}`) / 400 `product_required` / `tier_invalid` / `period_invalid` / 403 `role_not_allowed` (client на сетевую позицию); detail / 404; patch 200 / 400 `issued_count` только чтение / 409 `limit_below_issued`; delete = **архив**, 200 `{id, is_archived: true, is_active: false}` (не 204) |
| №55 подключение ВК | `vk_*` | `settings/vk/` сводка по сети до и после подключения (`branches[]`, `groups[]` с `secrets_consistent`); карточка точки get (пусто / подключено) / 404 чужая точка под client; `check/` 200 (группа, права, `missing_permissions`, `callback_servers[].ours`, `can_save`) / 403 client / **424** `vk_error` / **504** `vk_timeout`; patch: первое подключение без `confirm` (`vk_patch_201_connect` — ответ **200**, имя оставлено как записано), 400 `confirm_required` при смене токена, 400 неизвестное поле, 403 client, 409 `group_mismatch` (токен другой группы даже с `confirm`) |
| №56 флаги механик (запись) | `features_*` | get с `editable` / `readonly` / `branch_overridable`; patch сети 200; patch точки 200 (`branch_id` в теле: `birthday_window_days: 0` — настоящий ноль, сториз `0`/`""` — наследовать сеть); 400 `no_branch_override` / `readonly_flag` / `invalid_value` (тип) / `unknown_flag`; 403 client. `features_patch_404_foreign_branch_client` — на деле **403** `role_not_allowed`: client не пишет флаги вовсе, до проверки точки дело не доходит |
| №34 сводная по сетям | `overview_*` | `export/?period=7d` — CSV описан сводкой (`_content_type`, `_disposition`, `_length`, `_has_bom`, `_head` = заголовок 21 колонки + **синтетическая** строка данных); `sync-gift-costs/` 202 (`queued, task_id, scope, started_at, lock_seconds`) / 200 `dry_run` (`inventory, story, refilled_zeros, output_tail`) / 400 `confirm_required` / 409 `already_running` (`started_at`); `status/` (`running, last_run`); 403 `role_not_allowed` без признака платформы для export и sync |

Шероховатость записи: `rc_detail_404_foreign_client` — позиция точки 990002 **видна** её client'у (200); 404 под client даёт только позиция чужой точки
(в песочнице такой не заводили). `overview/*` живут на публичном urlconf и в срез `openapi_w2_2026-09-18.json` не попадают — форма только здесь и в контракте 3г.
