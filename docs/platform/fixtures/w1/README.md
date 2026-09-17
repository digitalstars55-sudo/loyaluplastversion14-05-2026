# Записанные ответы песочницы `dev` — волна 1 (для контрактных тестов CheckUp)

Записано 16.09.2026 на сети `dev` под токенами обмена (`network_admin` и `client` точки `990002`).
Каждый файл — один вызов: `request` (метод, путь, заголовки с плейсхолдерами секретов, тело) и
`response` (статус, тело). JWT в ответах заменён на `<jwt>`, настоящие гости обезличены
(VK ID → 1000000xx, имена → «Гость Тестовый», фото и телефоны стёрты, тексты из ВК скрыты);
синтетические отзывы песочницы (`[песочница CheckUp]`, VK ID 990000001+) оставлены как есть.

Как пользоваться: это **форма** ответов, не значения. Контрактный тест BFF CheckUp проверяет,
что его код читает поля из `response.body` этих файлов и правильно раскладывает коды ошибок
(`exchange_*`, `verdict_*`, `branch_patch_*`, `reviews_401_no_token`). Форма ответа меняется
только аддитивно; удаление поля — новая версия контракта.

Схема OpenAPI тех же ручек: `../../openapi_w1_2026-09-16.json` (внутренние ручки обмена и
вердикта — обычные Django-вьюхи, в схеме их нет, описаны в контракте 2.1 и 5.2).
Полная живая схема: `GET https://levelupapp.ru/api/schema/`.

Перезаписать: `scratchpad/record_fixtures.py` на проде через `manage.py shell` (всё в
транзакции с откатом) + обезличивание перед коммитом.

## Дополнение 18.09.2026 — рассылки (возможности 11, 13) и телефон гостя (№78)

- `broadcasts_*.json` — 25 записей на `dev` под теми же токенами обмена (`fx-admin` = `network_admin`, `fx-client` = `client` точки `990002`):
  черновик (`create_201`, `create_400_invalid_payload`, `create_403_branch_forbidden`, `create_201_client_own_point`, `list`, `list_client_scoped`,
  `detail`, `detail_404_foreign`, `patch_200`, `patch_400_invalid_status`, `delete_200`, `detail_404_deleted`), предпросмотр (`preview` — `count` возвращается
  как `expected_count`), отправка (`send_400_expected_count_required`, `send_400_confirm_required`, `send_409_audience_changed`, `send_200`,
  `send_409_already_sent`, `detail_after_send`, `patch_409_sent`, `delete_409_sent`), история и аварийные действия (`sends_list`, `sends_list_client_scoped`,
  `sends_list_400_bad_branch`, `send_cancel_404`). В `branch_ids` черновика — **внутренние** `id` точек (не публичные `branch_id`).
  Отправка в ВК при записи была **заглушена** (в песочнице настоящий токен сообщества): `sent/failed/skipped` в `send_200` и `sends_list` синтетические,
  очередь Celery не трогалась. Записи `cancel_200`, `edit-in-vk`/`delete-in-vk` (409 на незавершённой) не получились: в песочнице аудитория второй
  точки пуста, а без запуска в статусе pending/done их не снять — форма ответов этих трёх ручек описана в контракте (3.1, п. 13).
- `guest_card.json` — к записи 16.09 дописаны `phone`, `phone_source`, `phone_consent_at` по форме сериализатора (значения синтетические).
- Скрипт записи: `record_fixtures_w1b.py` (та же схема: транзакция с откатом, JWT → `<jwt>`, `unittest.mock.patch` на `run_broadcast` и `run_broadcast_task`).
