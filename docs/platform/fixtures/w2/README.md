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
