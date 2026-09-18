# Контракт платформы CheckUp × LoyalUP — v1.5

**Дата:** 16 сентября 2026 (v1), правки v1.1 — тот же день вечером по ревью агента CheckUp, v1.2 — `guest_vk_id` в жалобе по запросу CheckUp, v1.3 — 17 сентября, №78 телефон гостя сделан, v1.4 — 18 сентября, обмен открыт LevOne, правило «client — только чужим id», фикстуры рассылок, v1.5 — 18 сентября, старт волны 2 по слову владельца: раздел 3б, эталоны w2, срез схемы 18.09 · **Статус:** согласован обеими сторонами с правками; подпись владельца · **Для кого:** команда (агент) CheckUp, строящая модуль «Гости → LoyalUP»; владелец платформы

Этот документ — граница между двумя системами. Всё, что CheckUp делает с лояльностью, он делает через описанные здесь ручки и правила. Всё, что здесь не описано, для CheckUp не существует: ни таблиц LoyalUP, ни админки, ни гостевых ручек мини-аппа.

---

## Что изменилось в v1.1 (по ревью CheckUp 16.09)

Агент CheckUp сверил v1 не с документом, а с продом и кодом; архитектуру принял, четыре расхождения с фактами и два своих долга назвал. Всё учтено:

1. **Точки — один идентификатор на всю границу.** В обмене токена ходят публичные `branch_id` (те же, что в QR и жалобах), а не первичные ключи: у CheckUp в `LoyalupBranchMap` есть только он. Перевод во внутренний `id` LoyalUP делает сам.
2. **Роли — из кодов прав CheckUp.** У CheckUp нет owner/admin/manager/staff, права выдаются per-клиент кодами. CheckUp сам выводит из `loyalty.*` роль `network_admin` или `client` и присылает её; всё остальное — `403`. Это же закрывает вопрос про кассиров: «есть код — есть доступ».
3. **«Только loopback» → «только внутренний адрес».** Порт 7000 слушает через docker-proxy, контейнер видит источник как адрес моста `172.x`. Правило то же, что у релея жалоб: loopback или приватный адрес, без `X-Forwarded-For`.
4. **Одна личность на пару (сотрудник, сеть).** Роль у пользователя LoyalUP одна на все сети, у CheckUp — на каждого клиента своя; чтобы «админ» первой сети не протёк во вторую, пользователь LoyalUP заводится на пару: `checkup-<id>-<schema>`.
5. **Песочница `dev` существует** (домен и сертификат в порядке), но была пустой: одна точка, ни одного пользователя. Наполняется командой `seed_checkup_sandbox` (раздел 7).
6. **Ключи Anthropic разные, счёт общий.** Пункт волны 0 переформулирован: лечится лимитами на счёт или вторым счётом (владелец).
7. **Долги CheckUp записаны явно** (раздел 8): аналитика отзывов CheckUp уже смешивает поток LoyalUP с оценочными отзывами; спящий вебхук удалить, а не чинить; `FeatureCode.loyalty` в тарифах; BFF в Django, не в Next.js; кэш токена в Redis с TTL.
8. **Вместо ревью кода — контрактные тесты с обеих сторон** (раздел 9).
9. **Вердикт по жалобе (5.2) уточнён и сделан**: работает и на публичном хосте с `tenant_schema` в теле (как обмен), и на хосте сети; вердикт — поля карточки, не сообщение гостю; коды ошибок и `force`.

## Что изменилось в v1.2 (запрос CheckUp 16.09)

**В payload жалобы добавлен `guest_vk_id`** — публичный числовой VK ID автора жалобы (п. 5.1). До этого связаться с гостем из CheckUp было нельзя никак: имя есть, телефон есть только у части отзывов из мини-аппа, а у жалоб из ВК-тредов телефона нет вообще. Теперь в карточке жалобы CheckUp строит ссылку `https://vk.com/id<guest_vk_id>`. Поле необязательное (`null`, если ID не сложился), правка аддитивная — приёмник CheckUp принимает payload и без него, порядок выкладки сторон не важен. Диалог с гостем внутри CheckUp это не отменяет: он остаётся в волне 1 (№1 «Отзывы»).

---

## Что изменилось в v1.3 (17.09, ночь)

**`guest_vk_id` выкачен с обеих сторон.** LoyalUP — с 17.09 00:40 MSK (коммит 9f6c8ff), CheckUp — миграция guests 0018, веб и мобилка; первую живую жалобу с VK ID проверяет CheckUp.

**№78 «Телефон гостя с согласием через ВК» сделан целиком и включён на пилоте (dev, LevOne).**
- Гостевая ручка `POST/DELETE /api/v1/client/phone/` на хосте сети: тело — ответ bridge `VKWebAppGetPhoneNumber` как есть (`vk_id`, `phone_number`, `sign`). Подпись ВК `base64(sha256(app_id + secret + user_id + "phone_number" + phone))` проверяется по нескольким кодировкам дайджеста; первые живые запросы в режиме наблюдения (лог `sign=ok:<вариант>`), потом строгий режим `GUEST_PHONE_SIGN_ENFORCE=on`. Чужому гостю номер вписать нельзя: нужна подпись ВК или доказанный заголовок запуска.
- У гостя (`guest.Client`, общая таблица): `phone` (E.164), `phone_source` (`vk` — подпись сошлась, `vk_unverified` — наблюдение), `phone_consent_at`. **Для CheckUp:** карточка гостя №7 отдаёт `phone`, `phone_source`, `phone_consent_at`; в жалобе (5.1) `guest_phone` берётся из профиля, если гость не написал телефон в отзыве. Ключ склейки 5.4 по E.164 теперь есть у всех гостей, давших номер.
- Мини-апп: кнопка «Поделиться номером» в профиле → панель согласия со ссылкой на политику `levelupapp.ru/privacy` (правило ВК 1.1.4; в политику добавлен раздел о гостях мини-приложения) → окно согласия ВК → номер на месте кнопки с «убрать» (отзыв согласия, 152-ФЗ). Сборка на проде 17.09 (бандл `index-COwjYXBG.js`).
- Флаги: общий выключатель платформы `GUEST_PHONE_ENABLED` (env) **и** флаг сети `ClientConfig.guest_phone_enabled` (админка LoyalUP). Включены оба у `dev` и `LevOne`; у остальных девяти сетей кнопки нет, поведение прежнее.

**Что осталось на стороне LoyalUP по контракту:** приёмка волны 1 на `dev`, затем пилот LevOne — когда у CheckUp появятся экраны; включение обмена токена живым сетям (решение владельца); строгий режим подписи запуска `VK_SIGN_ENFORCE` после разбора запросов мини-аппа без заголовка; хвосты волны 0 (секрет доставки Dooglys, перенос статики мини-аппа на `levelupapp.ru`); техдолг миграции `users.PushToken`.

## Что изменилось в v1.4 (18.09, по приёмке CheckUp §7)

1. **Обмен токена открыт LevOne** 17.09 22:45 MSK по слову владельца: `CHECKUP_TOKEN_EXCHANGE_TENANTS=dev,levone`, секрет обмена общий у двух сторон. Автосуши и Шавуха — по отдельному слову владельца (п. 10.5 закрыт).
2. **Правило «проверки под ролью `client` — только чужим id»** (риск нашёл агент CheckUp). Обмен обновляет роль и точки ровно по телу, а права читаются из пользователя на каждом запросе: обмен с `role: "client"` для `checkup_user_id` живого сотрудника, который уже работал как `network_admin`, понижает его немедленно — и уже выданный JWT у BFF (до 60 минут) тоже становится «клиентским». Поэтому проверки и контрактные тесты ходят только с синтетическими id (`fx-…`, `test-…`, как в фикстурах), никогда с id живого сотрудника; при смене роли или точек сотрудника в CheckUp BFF сбрасывает кэш токена по ключу `(tenant_schema, checkup_user_id)` и делает новый обмен. Отказ обмена «по факту понижения» LoyalUP **не вводит**: источник истины по ролям — CheckUp, и понижение в CheckUp обязано доехать до LoyalUP тем же обменом (пп. 2.1, 4.16).
3. **Известные шероховатости волны 1 записаны как есть** (3.2): три формы ошибок, `vk_id` строка/число, значения `period` у сводки отзывов, отсутствие ручки одного отзыва и пагинации у гостей RF-ячейки и сообщений треда. Ручка одного отзыва и внутренний `id` точки **сделаны 18.09 (коммит 461689d, на проде с 00:40 MSK)**: `GET /api/v1/mobile/reviews/{id}/` — та же карточка, что элемент списка, чужой или несуществующий тред → `404`; в ответе обмена поле `branches` — `[{"branch_id": <публичный>, "id": <внутренний>}]` для `client` в порядке присланных `branch_ids`, `null` для `network_admin` (= все точки; их список с `branch_id` рядом с `id` теперь отдаёт `GET /api/v1/analytics/branches/`).
4. **Фикстуры дополнены**: `fixtures/w1/broadcasts_*.json` (черновик, права по точке, предпросмотр, отправка с `expected_count`, `409 audience_changed`, история запусков, отмена и аварийные действия) и `guest_card.json` перезаписан с `phone`, `phone_source`, `phone_consent_at`. Отправка в ВК при записи заглушена — цифры `sent/failed` синтетические.

---

## Что изменилось в v1.5 (18.09, старт волны 2)

Владелец сказал «давай волну 2». Сторона CheckUp сверила волну 1 на проде по v1.4 (совпало) и назвала, что нужно её экранам по №31 и №26/27; сторона LoyalUP пересверила карту по коду. Итог:

1. **Раздел 3б «Ручки волны 2»** — 20 возможностей (15 из корзины A + 5 бывших «экспертных»): что уже есть, что добавляем и в каком порядке (31 → 26/27 → 28 → 23 → 29 → 52/56; 49/55 — только по отдельному слову владельца). Формы всех новых ручек (3б.1–3б.7) записаны по разведке кода 18.09 и ждут ревью CheckUp; оценка волны 2 на стороне LoyalUP ≈ 17 дней.
2. **Эталоны для готовых ручек волны 2** записаны: `fixtures/w2/` (45 записей на `dev`, README там же). Формы новых ручек (3б.1 и далее) — сначала здесь, на ревью CheckUp, потом код; их эталоны появятся вместе с кодом.
3. **Срез схемы перегенерирован 18.09** (`openapi_w1_2026-09-18.json`, `loyalup_w1_2026-09-18.d.ts`): добавлены `broadcasts/*` и карточка отзыва, лента и сообщения описаны объектами, query-параметры на месте. Причина дефекта 16.09 — декоратор схемы висел на `list`, а не на HTTP-методе; исправлено в коде.
4. **№53 «Ссылки на карты» закрыт без новых ручек** — это поля `PATCH /api/v1/mobile/branches/{id}/` (`review_link_yandex`, `review_link_2gis`, `review_links_default`, `yandex_map`, `gis_map`), см. №15.
5. **Шероховатости готовых ручек волны 2** дописаны в 3.2 (форма `{"error"}`, английские 404 DRF, `204` без тела, каталог/квесты/акции без прав по точкам).
6. **QR-картинки в v1.5 не отдаём** — только `url`; CheckUp рисует QR на фронте (как админка LoyalUP). Ручка PNG/SVG — отдельный пункт после решения владельца о зависимости.
7. **Попутно закрыта дыра** в веб-кабинете LoyalUP: детализация «Точек контакта» (`/analytics/contact-points/detail/?qr=`) не проверяла доступ к точке — сотрудник одной точки видел гостей чужой (нашла разведка №26/27; чужая точка → `404`).

## 1. Принципы

1. **LoyalUP остаётся сервисом гостей и источником истины по лояльности.** Гости, баллы, отзывы, рассылки, RF-сегменты живут в LoyalUP. Переезжают экраны сотрудника, не данные.
2. **Граница — HTTP API LoyalUP на домене сети:** `https://<schema>.levelupapp.ru/api/v1/…`. Никакого доступа к базе LoyalUP, никаких общих таблиц, никаких файлов на диске.
3. **Браузер CheckUp в LoyalUP не ходит.** В волне 1 все вызовы идут через BFF CheckUp (сервер → сервер). CORS для `checkupapp.ru` уже открыт как запас на будущее (виджеты), но контракт v1 его не использует.
4. **Тенант определяется по `Host`.** Заголовка выбора сети нет и не будет: запрос на `levone.levelupapp.ru` — это сеть LevOne, точка. Одной организации CheckUp может соответствовать несколько сетей LoyalUP (у Автосуши: `asap_bryansk` и `asap_orel`) — переключатель организации в CheckUp ≠ переключатель сети лояльности.
5. **Всё новое — под флагами.** На стороне LoyalUP новые ручки включаются переменными окружения (выключено = прод как раньше). На стороне CheckUp модуль включается по тарифу/флагу клиента; клиенты без модуля ничего не видят.
6. **Опасные действия защищены на бэкенде LoyalUP,** а не в интерфейсе CheckUp: предохранитель аудитории рассылки, права по точкам, лимиты ВК. CheckUp обязан их уважать, но не обязан дублировать.
7. **Стык держат контрактные тесты с обеих сторон, а не ревью кода** (раздел 9). Отклонения от контракта — крупно в отчёт, а не молча.

---

## 2. Как CheckUp получает доступ от имени сотрудника

### 2.1. Обмен токена (новая ручка LoyalUP)

Сотрудник залогинен в CheckUp. Когда он открывает раздел лояльности, BFF CheckUp меняет свою личность сотрудника на короткоживущий JWT LoyalUP:

```
POST http://127.0.0.1:7000/api/v1/internal/auth/exchange/
Host: levelupapp.ru
X-LoyalUP-Exchange-Secret: <CHECKUP_TOKEN_EXCHANGE_SECRET>
Content-Type: application/json

{
  "checkup_user_id": "812",
  "tenant_schema": "levone",
  "role": "client",
  "branch_ids": [1, 2],
  "display_name": "Алина Петрова",
  "email": "manager@levone.ru"
}
```

Ответ `200`:

```json
{
  "token": "<JWT>",
  "expires_at": "2026-09-16T18:05:00+00:00",
  "expires_in": 3600,
  "tenant_schema": "levone",
  "tenant_domain": "levone.levelupapp.ru",
  "created": false,
  "branches": [{"branch_id": 1, "id": 11}, {"branch_id": 2, "id": 12}],
  "profile": { "id": 57, "username": "checkup-812-levone", "role": "client", "role_label": "Клиент",
               "full_name": "Алина Петрова", "branch_ids": [], "tenant_domain": "levone.levelupapp.ru",
               "tenant_name": "LevOne", "is_superadmin": false }
}
```

Правила:

- **Только с того же сервера.** Ручка живёт на публичном хосте (`Host: levelupapp.ru`, как релей жалоб), сеть передаётся в теле — `tenant_schema` (имя схемы LoyalUP: `levone`, `asap_orel`, `dev`…). Отвечает `403`, если запрос пришёл не с внутреннего адреса: loopback **или приватный адрес** (docker-мост `172.x`), без `X-Forwarded-For` — то же правило, что у `internal/support/inbound-reply/`. Секрет — отдельный, `CHECKUP_TOKEN_EXCHANGE_SECRET`; секрет жалоб (`LOYALUP_RELAY_SECRET`) сюда не подходит. Пустой секрет на стороне LoyalUP = ручка выключена (`503 exchange_disabled`). Дополнительно белый список сетей `CHECKUP_TOKEN_EXCHANGE_TENANTS`: на проде сначала только `dev`, живые сети — по решению владельца (`403 tenant_not_allowed`).
- **Роль присылает CheckUp.** У CheckUp нет ролей owner/admin/manager/staff — права per-клиент кодами. CheckUp сам выводит из своих кодов `loyalty.*` одну из двух ролей LoyalUP и передаёт её в `role`: `network_admin` (все точки сети) или `client` (только `branch_ids`). Любое другое значение → `403 {"code": "role_not_allowed"}`. Кассиру доступ даёт или не даёт код права в CheckUp, а не этот контракт.
- **Точки — публичные `branch_id`.** `branch_ids` — те же числа, что в QR-ссылках и в жалобах (`loyalup_point_id` у CheckUp); один идентификатор на всю границу. LoyalUP сам переводит их во внутренние `id` для `branch_access`. Неизвестный `branch_id` → `422 {"code": "unknown_branch", "unknown_branch_ids": [...]}`. Для `client` список обязателен и не пуст; для `network_admin` игнорируется (все точки). Соответствие филиал CheckUp → точка LoyalUP берётся из `LoyalupBranchMap`; **адресный матчинг запрещён**.
- **Личность — на пару (сотрудник, сеть).** LoyalUP ведёт таблицу `CheckUpIdentity (checkup_user_id, tenant_schema) → User`. Первый обмен создаёт пользователя `checkup-<id>-<schema>` без пароля; последующие обновляют имя, роль и точки ровно по телу запроса (в том числе понижение). Роль у пользователя LoyalUP одна на все сети, поэтому один сотрудник CheckUp с двумя сетями — это два пользователя LoyalUP. Email хранится только в таблице соответствия для показа; **автосклейки по email нет**, в `User.email` он не пишется (иначе сломал бы вход по email «родному» сотруднику). Пользователя, которого оператор LoyalUP выключил, обмен не оживляет — `403 user_disabled`. **Следствие (v1.4):** обмен — единственный источник роли, поэтому «проверочный» обмен с `role: "client"` по id живого сотрудника понизит его немедленно, включая уже выданный JWT; для проверок — только синтетические id (п. 4.16).
- **`branches` (с 18.09, v1.4)** — пары публичный `branch_id` → внутренний `id` точек сотрудника, в порядке присланных `branch_ids`; у `network_admin` — `null` (все точки сети, список с `branch_id` — `GET /api/v1/analytics/branches/`). Поле необязательное, старые поля не менялись.
- **Токен живёт 60 минут, refresh не выдаётся.** Истёк — BFF делает новый обмен. В браузер токен не попадает никогда. Кэш на стороне BFF — Redis с TTL не больше `expires_in`, ключ `(tenant_schema, checkup_user_id)`; «в памяти процесса» при нескольких воркерах даёт обмен на каждый воркер.
- **Ошибки:** `400 invalid_json` · `401 bad_secret` · `403 forbidden` (внешний адрес) / `role_not_allowed` / `tenant_not_allowed` / `user_disabled` · `404 tenant_not_found` / `tenant_inactive` · `409 identity_conflict` (имя `checkup-<id>-<schema>` занято «родным» пользователем) · `422 invalid_payload` / `unknown_branch` · `503 exchange_disabled`. Тело всегда `{"code": "...", "detail": "..."}`.

### 2.2. Дальше — обычный JWT LoyalUP

Все ручки раздела 3 принимают `Authorization: Bearer <token>`. Права по точкам (`branch_access`) LoyalUP проверяет сам: чужая точка в фильтре → `403 {"detail": "Нет доступа к выбранным точкам"}` или пустой результат. Права по разделам (`feature_access`, 18 ключей) LoyalUP в API **не проверяет** — CheckUp гейтит разделы сам по своим кодам `loyalty.*` (в `profile` у пользователей `checkup-*` `feature_access` пуст = «все разделы роли»). Это осознанно: в LoyalUP разделы гейтил веб-кабинет, а не API, и менять это в волне 1 не будем.

### 2.3. Как ходить в LoyalUP с того же сервера

BFF CheckUp живёт в Django (мобилка CheckUp ходит только в Django API) и ходит **напрямую в gunicorn** — `http://127.0.0.1:7000` — а не через публичный nginx. Причины: (а) публичный домен `.levelupapp.ru` целиком стоит под лимитом nginx 8 запросов/с на IP (все запросы BFF шли бы с одного адреса и получали `429`); (б) TLS и DNS внутри одного сервера не нужны. Заголовок `Host` выбирает схему: обмен токена и релей — `Host: levelupapp.ru` (публичная схема), ручки раздела 3 — `Host: <schema>.levelupapp.ru`. Порт 7000 снаружи закрыт (DROP на `eth0`), но слушает на `0.0.0.0` через docker-proxy — поэтому правило доступа «внутренний адрес», не «loopback». Проверка одна: `GET /api/v1/branches/239014483/` с `Host: dev.levelupapp.ru` отдаёт `200`.

Таймауты BFF: чтение 10 с, отправка рассылки/кампании 30 с. Повторять автоматически можно только `GET`; `POST`/`PATCH`/`DELETE` — нет (см. 4.10).

---

## 3. Ручки волны 1

Источник истины по полям ответов — OpenAPI LoyalUP: `GET https://levelupapp.ru/api/schema/` (Swagger: `/api/docs/`). Отдельно LoyalUP отдаёт CheckUp сгенерированные TypeScript-типы для ручек этой таблицы (см. раздел 8). Общие правила: JSON, даты в ISO-8601 с часовым поясом, `limit`/`offset` там, где пагинация есть; идентификатор точки в этих ручках — `id` (первичный ключ), поле `branch_id` в ответах — публичный номер точки для QR и ссылок, в фильтры его не передавать.

| № | Возможность | Ручки LoyalUP (все под JWT) | Готовность | Что LoyalUP добавляет к волне 1 |
|---|---|---|---|---|
| 1 | Отзывы: лента, карточка, ответ, черновик ИИ, автоответ | `GET /api/v1/mobile/reviews/` · `GET /api/v1/mobile/reviews/{id}/` (с 18.09, та же карточка) · `GET /api/v1/mobile/reviews/{id}/messages/` · `POST …/{id}/reply/` · `POST …/{id}/resolve/` · `POST …/{pk}/cancel-auto-send/` · `POST /api/v1/analytics/reviews/{id}/regenerate-draft/` · `POST …/{id}/reject-draft/` | готово (16.09) | ✅ сделано 16.09: `limit/offset` (+ `total` в ответе), `sentiment` (в т.ч. `bad` = весь негатив), `status=unread\|replied\|unanswered`, `source=app\|vk`, `checkup_status`, `q` (VK ID / имя); без параметров ответ прежний |
| 2 | Сводка репутации и рейтинг точек | `GET /api/v1/mobile/branches/` (рейтинг и число отзывов по точке) | готово (16.09) | ✅ `GET /api/v1/analytics/reviews/summary/?period=&branch_ids=` — тональности, рейтинг, источники, негатив без ответа, по точкам, статусы CheckUp; цифры теми же запросами, что веб-страница «Анализ отзывов» |
| 3 | Настройки автоответов ИИ | `GET/PATCH /api/v1/analytics/auto-reply/settings/` | готово | — |
| 4 | Дашборд дня («Задачи дня») | сейчас собирается клиентом из `GET /api/v1/analytics/rf/`, `GET /api/v1/billing/status/`, `GET /api/v1/branch/daily-codes/` | готово (16.09) | ✅ `GET /api/v1/dashboard/today/?branch_ids=` — негатив без ответа, ждут ответа, черновики ИИ, автоответы в очереди, новые за день, жалобы в работе; коды дня и точки без кода; срок оплаты; топ точек за 30 дней |
| 5 | Общая статистика программы | `GET /api/v1/analytics/stats/` · `GET /api/v1/analytics/stats/slow/` · `GET /api/v1/analytics/branches/` | готово (16.09) | ✅ `GET /api/v1/analytics/stats/detail/?metric=&period=&limit=&offset=` — те же гости, что веб-страница `/analytics/stats/detail/`; неизвестная метрика → `400` со списком ключей |
| 6 | База гостей | `GET /api/v1/guests/?search=&limit=&offset=` | готово | — |
| 7 | Карточка гостя | `GET /api/v1/guests/{vk_id}/` | готово | поля `phone`, `phone_source` (`vk` / `vk_unverified` / `review`), `phone_consent_at` — **есть с 17.09 (№78)**; телефон из профиля гостя, иначе из последнего отзыва |
| 8 | Корректировка баллов | `POST /api/v1/guests/{vk_id}/adjust-coins/` (причина обязательна) | готово | — |
| 9 | RF-матрица и сегменты | `GET /api/v1/analytics/rf/` · `POST /api/v1/analytics/rf/recalculate/` · `GET /api/v1/analytics/segments/` | готово | — (пересчёт тяжёлый и синхронный: показывать спиннер, не дёргать чаще раза в час) |
| 10 | Миграции гостей между сегментами | `GET /api/v1/analytics/rf/migrations/` | готово | — |
| 11 | Рассылка по сегменту / всем | `POST /api/v1/analytics/rf/send-broadcast/` · `POST /api/v1/analytics/rf/generate-broadcast-text/` | готово (16.09) | ✅ `GET/POST /api/v1/broadcasts/`, `GET/PATCH/DELETE …/{id}/`, `GET …/{id}/preview/` (`count` → `expected_count`), `POST …/{id}/send/` `{expected_count, confirm: true}` — оба обязательны; расхождение больше max(5, 10 %) **в любую сторону** → `409 {"code": "audience_changed", "expected", "actual"}`; история `GET /api/v1/broadcasts/sends/` с правами по точке. Картинка в v1 не поддерживается |
| 12 | История рассылок и отклик | `GET /api/v1/analytics/campaigns/` · `PATCH/DELETE /api/v1/analytics/campaigns/{id}/` | готово | — (прочтения проставляются фоновой задачей раз в час: «0 прочитано» сразу после отправки — норма) |
| 13 | Аварийное управление отправленной рассылкой | нет (только HTML-подтверждения в админке) | готово (16.09) | ✅ `POST /api/v1/broadcasts/sends/{id}/cancel/` (только pending/running; до 10 сообщений ещё может уйти), `…/edit-in-vk/` и `…/delete-in-vk/` (только done с отправленными, окно ВК 24 ч). Все три требуют `confirm: true`; чужая рассылка → `404` |
| 14 | RFM-кампании (награда сегменту) | `GET/POST /api/v1/analytics/rf/campaigns/` · `GET …/{id}/` · `POST …/{id}/cancel/` · `GET …/kpi/` · `GET /api/v1/analytics/rf/reward-catalog/` | готово | — (создание тоже требует `expected_count`) |
| 15 | Точки сети | `GET /api/v1/mobile/branches/` | готово (16.09) | ✅ `GET/PATCH /api/v1/mobile/branches/{id}/` — карточка и частичная правка: контакты, адрес, карты, ссылки на отзывы, тексты подсказок; только `network_admin`; `branch_id`/`is_active` только чтение (`400` со списком редактируемых полей) |
| 16 | Сотрудники и права | `GET /api/v1/staff/` · `PATCH/DELETE /api/v1/staff/{id}/` · `POST /api/v1/staff/invite/` · `POST /api/v1/staff/link-existing/` | готово | в волне 1 **не переносим**: пользователей, созданных через обмен токена, ведёт CheckUp; ручки нужны только для «родных» сотрудников LoyalUP в переходный период |
| 17 | Уведомления и пуши | `GET/POST /api/v1/notifications/` · `GET/PATCH /api/v1/me/push-prefs/` · `POST/DELETE /api/v1/push/register/` | готово (16.09) | ✅ `GET /api/v1/notifications/?limit=&before_id=&since=&type=&unread=1` → `has_more`, `next_before_id`; без параметров ответ прежний. Пуши на телефоны сотрудников CheckUp LoyalUP **не шлёт**: события уходят в колокольчик CheckUp (раздел 5.3) |
| 78 | Телефон гостя с согласием через ВК | нет | нет | **сделано 17.09**: гостевая ручка `POST/DELETE /api/v1/client/phone/` (проверка подписи ВК, наблюдение → enforce), мини-апп (панель согласия со ссылкой на политику → окно ВК → кнопка), флаги: общий `GUEST_PHONE_ENABLED` + по сети `ClientConfig.guest_phone_enabled` (пилот dev, LevOne); для CheckUp виден результат в карточке гостя (№7) и `guest_phone` в жалобе (5.1) |

Ручки «Коды дня» (`GET /api/v1/branch/daily-codes/`, `POST …/generate/`) в волне 1 нужны только виджету дашборда — полный экран в волне 2.

### 3.1. Коды ответов, одинаковые для всех ручек

| Код | Когда | Тело |
|---|---|---|
| `400` | не прошла валидация | `{"<поле>": ["текст"]}` — стандарт DRF; показывать текст у поля |
| `401` | нет/протух JWT | `{"detail": "…"}` — BFF делает новый обмен и повторяет один раз |
| `403` | чужая точка, роль, `role_not_allowed` | `{"detail": "…"}` или `{"code": "…"}` — показать как есть, не повторять |
| `404` | объект не в этой сети | помнить про `Host`: тот же id в другой сети — другой объект |
| `409` | аудитория рассылки/кампании разошлась с `expected_count` | новые ручки `/api/v1/broadcasts/…`: `{"code": "audience_changed", "expected": N, "actual": M, "detail": "…"}` (допуск max(5, 10 %) в любую сторону); старые `rf/send-broadcast` и `rf/campaigns`: `{"error": "…"}` — в обоих случаях показать новый охват и попросить подтвердить заново; **автоповтор запрещён** |
| `429` | лимит nginx | не должно случаться при ходе через loopback (2.3); если случилось — BFF ходит не туда |
| `5xx` | сбой LoyalUP | показать «сервис лояльности недоступен», не ретраить `POST` |

### 3.2. Известные шероховатости волны 1 (как есть; v1.4)

Найдены агентом CheckUp на приёмке §7. В волне 1 не переделываем — форма ответов меняется только аддитивно; BFF CheckUp закладывает это в маппинг.

| Что | Как есть | Что делает BFF |
|---|---|---|
| Три формы ошибок | `{"detail": "…"}` у `mobile/*`; `{"error": "…"}` у старых `analytics/rf/*` (400/409); словарь `{"<поле>": ["…"]}` от DRF на валидации; `{"code": "…", "detail": "…"}` у новых ручек (`broadcasts/*`, `dashboard/*`, `internal/*`) | один маппер: текст = `detail` → `error` → первое значение словаря; `code` — если есть |
| `vk_id` | строка в `GET /guests/` и карточке гостя; число в `analytics/stats/detail/` и в `guest_vk_id` жалобы | приводить к строке, сравнивать как строки |
| `period` сводки отзывов | `analytics/reviews/summary/` принимает только `7d`, `30d`, `all`; число дней → `400` | в остальных ручках `period` — число дней (`mobile/reviews/?period=365`) |
| Один отзыв | ~~ручки нет~~ — с 18.09 есть `GET /mobile/reviews/{id}/`: ровно та же карточка, что элемент `reviews` в списке (общие база, RBAC и сериализатор); чужой тред → `404`, не `403` | открывать отзыв по ссылке из жалобы, не выкачивая ленту |
| `branch_id` в двух смыслах | в элементах `mobile/reviews/*`, карточке гостя (`recent_visits`), черновиках и запусках `broadcasts/*` поле `branch_id` — это **внутренний** `id` точки (FK); в `analytics/branches/`, в `branches` ответа обмена, в `branch_ids` запроса обмена, в QR и жалобах `branch_id` — **публичный** номер. Ориентир: если рядом в объекте есть `id`, то `branch_id` публичный; если `branch_id` один — внутренний. `dashboard/today` отдаёт оба: `branch_id` (внутренний) и `public_branch_id` | сводить точки по внутреннему `id` из `branches` обмена / `analytics/branches/`; по имени точки не сводить |
| Пагинация | нет у гостей RF-ячейки (`analytics/rf/…`) и у сообщений треда (`…/messages/`) | показывать первые N и писать «показаны последние N» (п. 4.12); не тянуть циклами |
| Внутренний `id` точки | с 18.09 в ответе обмена `branches: [{branch_id, id}]` у `client` (порядок = `branch_ids` запроса) и `branches: null` у `network_admin`; полный список с `branch_id` — `GET /analytics/branches/` (`[{id, branch_id, name}]`) | брать `id` для фильтров ручек раздела 3 из `branches`, `null` = все точки |
| Ошибки готовых ручек волны 2 | у `catalog/*`, `quests/*`, `branch/promotions/*`, `branch/daily-codes/generate/`, `assistant/ask/` валидация — `{"error": "…"}` (400/404 «Точка не найдена»); `404` по неизвестному `id` в `PATCH/DELETE` — стандарт DRF по-английски (`{"detail": "No Product matches the given query."}`); `DELETE` → `204` без тела | тот же маппер ошибок (п. 1 таблицы); английские 404 переводить у себя |
| Права по точкам у готовых ручек волны 2 | есть у кодов дня, дней рождения, вовлечённости (`*_client_scoped` в `fixtures/w2/`); **нет** у каталога, категорий, квестов и акций — роль `client` видит и правит всё по сети | до доделки LoyalUP (3б, «ограничение по точкам для client», ~0,5 дня) гейтить правами `loyalty.manage` и точками сотрудника у себя; после — снять дубль |
| Лояльчик | `POST /api/v1/assistant/ask/` зовёт Claude на ключе прода — каждый вызов стоит кредитов; при отсутствии ключа `503`, при сбое `502` `{"error"}` | не дёргать автоматически, только по действию человека; в контрактных тестах — эталон `assistant_ask_200` (ответ при записи заглушен) |

---

## 3б. Ручки волны 2 (v1.5)

Волна 2 по карте переезда (принята CheckUp 18.09, уточнения — 3б.8) — **20 возможностей**: №18–32 из корзины A и пять бывших «экспертных» (49, 52, 53, 55, 56). Порядок работ LoyalUP по новым ручкам: **31 → 26/27 → 28 → 23 → 29 → 52/56**; CheckUp начинает экраны с готовых ручек (18, 25, затем 19/20, 21/22, 24/30/32). Правила те же, что в разделе 3: JWT из обмена, `Host` сети, внутренний `id` точки в фильтрах, публичный `branch_id` рядом в объектах.

| № | Возможность | Ручки LoyalUP (все под JWT) | Готовность | Что LoyalUP делает в волне 2 |
|---|---|---|---|---|
| 18 | Коды дня | `GET /api/v1/branch/daily-codes/` · `POST …/generate/` `{branch_id, purpose: BIRTHDAY\|SUPERPRIZE\|…}` | готово | эталоны `fixtures/w2/daily_codes_*` (значения кодов синтетические) |
| 19 | Каталог подарков | `GET/POST /api/v1/catalog/products/` · `PATCH/DELETE …/{id}/` (картинка — multipart `image` на тот же URL; `assignments: [{branch_id, category_id, ordering, is_visible}]`) | готово | ограничение по точкам для роли `client` (см. 3.2); эталоны `catalog_products_*` |
| 20 | Категории каталога | `GET/POST /api/v1/catalog/categories/?branch_ids=` · `PATCH/DELETE …/{id}/` | готово | то же; эталоны `catalog_categories_*` |
| 21 | Акции и промо-баннеры | `GET/POST /api/v1/branch/promotions/` · `PATCH/DELETE …/{id}/` (`{branch_id, title, discount, dates}`, картинка multipart `image`) | готово | то же; эталоны `promotions_*` |
| 22 | Квесты | `GET/POST /api/v1/quests/` · `PATCH/DELETE …/{id}/` (`{name, description, reward, branch_ids \| all_branches, is_active, ordering}`) | готово | то же; эталоны `quests_*` |
| 23 | Механика «Игра через сториз» | нет (только админка, настройки в трёх местах) | нет | **добавить** `GET/PATCH /api/v1/settings/story/` (сеть) и `GET/PATCH /api/v1/mobile/branches/{id}/story/` (переопределение точки, `effective` + `source`) — формы в 3б.3 (2 дня) |
| 24 | Аналитика вовлечённости | `GET /api/v1/analytics/engagement/?period_days=&branch_id=` | готово | эталоны `analytics_engagement*` |
| 25 | Дни рождения гостей | `GET /api/v1/guests/birthdays/?days_ahead=&include_past=` | готово | эталоны `guests_birthdays*` (гости обезличены) |
| 26 | Точки контакта (QR) и воронка | воронка — `GET /api/v1/analytics/contact-points/` (остаётся мобилке; `branch` там — имя строкой) | частично | **добавить** `/api/v1/contact-points/…` — список с воронкой и сканами, карточка с воронкой по дням, гости стадии, `POST/PATCH/DELETE`, `batch-tables` — формы в 3б.1 (в работе, ~2 дня на 26+27) |
| 27 | Материалы для гостей: ссылки и QR | нет (change_form админки) | нет | **добавить** `GET /api/v1/mobile/branches/{id}/materials/` — готовые ссылки и списки QR по режимам; картинка QR — на фронте по `url` (3б.1) |
| 28 | Отчёт по системе лояльности | `GET /api/v1/analytics/report/` · `POST …/report/generate-comment/`; PDF — HTML-страница по `?token=` | частично | **добавить** комментарии в базе (`…/report/comments/`, `…/report/sections/`), `comments` в JSON, PDF-версия под JWT (`…/report/print/`) — формы в 3б.4 (3 дня) |
| 29 | AI-маркетолог: дайджест и посты | нет (экшены админки) | нет | **добавить** `/api/v1/marketer/settings/` (без токена стены), `/api/v1/marketer/posts/` + generate / publish (`confirm`) / reject / context — формы в 3б.5 (2,5 дня) |
| 30 | AI-ассистент «Лояльчик» | `POST /api/v1/assistant/ask/` `{question, history?}` → `{answer, actions[]}` · `GET /api/v1/assistant/context/` | готово | эталоны `assistant_*` (см. 3.2 про кредиты) |
| 31 | Авторассылки (правила по событиям) | `GET /api/v1/auto-broadcasts/` · `PATCH …/{id}/` (только `message_text`, `is_active`) · `GET …/{id}/preview/` | частично | **добавить** полный конструктор: справочник событий, создание, все поля, архив, A/B-варианты, preview с `count`, `activate/` с `expected_count`, лог, статистика, тест-отправка — формы в 3б.2 (4–5 дней) |
| 32 | Связь с персональным менеджером | `GET /api/v1/support/chat/manager/` | готово | эталон `support_chat_manager` |
| 49 | Каталог наград (пул призов RFM и авторассылок) | `GET /api/v1/analytics/rf/reward-catalog/` (чтение) | частично | CRUD — **только по отдельному слову владельца** и с предохранителями на бэке (пул призов = деньги сети) |
| 52 | База знаний для ИИ | нет | нет | **добавить** `/api/v1/ai/knowledge/` (список, загрузка docx/txt, включение, текст «что видит ИИ») — 3б.7 (1–1,5 дня), после 29 |
| 53 | Ссылки на карты (Яндекс / 2ГИС) точки | `PATCH /api/v1/mobile/branches/{id}/` (`review_link_yandex`, `review_link_2gis`, `review_links_default`, `yandex_map`, `gis_map`) | **готово** (закрыт 18.09 пересверкой) | — |
| 55 | Подключение ВКонтакте | нет | нет | **не в v1.5**: токены сообществ — только по слову владельца, с предохранителями (ошибка = отключение рассылок сети) |
| 56 | Флаги механик тенанта | часть в гостевой `GET /api/v1/company/<client_id>/` | частично | **добавить** `GET /api/v1/settings/features/` **только на чтение** по белому списку (3б.7, 1 день); запись — по слову владельца |

### 3б.1. №26/27 — точки контакта (QR) и материалы (согласовано с CheckUp 18.09; код в работе)

Новый модуль рядом со старой воронкой `/analytics/contact-points/` (её не меняем — на ней мобилка). Сущность — `QRCode` LoyalUP: `mode` **наш**: `cafe` (у CheckUp «hall»), `delivery`, `delivery_network` (сетевой QR доставки, точку выбирает код), `website`, `review` (стол; «table» у CheckUp) + `mode_label`; значения `story` нет — сториз не точка контакта, а источник подписки и вход по коду дня. `src` = ключ QR, генерируется LoyalUP, в запросах не принимается (на нём печать). Миграций нет.

| Ручка | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/contact-points/?branch_ids=&mode=&is_active=&q=&period\|start&end&limit&offset` | фильтры по внутренним `id` точек | `{total, limit, offset, results: [row], totals: {scans, guests, subscribed, played, activated, conversion}, meta: {start, end, branch_ids}}` — `totals` считаются **по показанной странице** (шапка сходится с таблицей; итоги по всему фильтру — по запросу CheckUp вторым полем); `row = {id, name, branch: {id, branch_id, name}, mode, mode_label, table_number, src, url, is_active, created_at, scans: {d7, d30, all}, funnel: {scans, guests, subscribed, played, activated, conversion}}` |
| `GET /api/v1/contact-points/{id}/` | — | `row` + `funnel_by_day: [{date, scans, guests}]` (30 дней или `period`) |
| `GET /api/v1/contact-points/{id}/guests/?stage=scan\|subscribe\|play\|activate&period&limit&offset` | стадия воронки (у CheckUp «scans» и «guests» в списке — одно множество, различие только в счётчиках) | `{total, limit, offset, stage, stage_label, results: [{guest_id, vk_id, name, at, segment: {code, name}\|null, branch: {id, branch_id, name}}], meta: {start, end}}` — строка на **гостя** (`guest_id` = id гостя сети, не профиля в точке: так `total` сходится с `funnel.guests`), `at` = время последнего события гостя на стадии |
| `POST /api/v1/contact-points/` | `{branch_id, name, mode, table_number?}` (`table_number` обязателен при `mode=review`); поля `src`/`key` в теле → `400 invalid_payload` (метку задаёт только LoyalUP, молча не игнорируем) | `201 row` · `400 invalid_payload` / `table_required` · `404 not_found` (недоступная точка — тоже 404) |
| `PATCH /api/v1/contact-points/{id}/` | `{name?, is_active?, mode?, branch_id?, table_number?}` | `row` (всегда с пересобранной `url`) · `409 has_scans` при смене `mode`/точки/стола после сканов · `404` |
| `DELETE /api/v1/contact-points/{id}/` | — | `204` · `409 has_scans` (сканы или события есть — история воронки не удаляется) · `404` |
| `POST /api/v1/contact-points/batch-tables/` | `{branch_id, from, to, name_template?}` (по умолчанию «Отзыв со стола {table}»; потолок 200 за вызов) | `201` (или `200`, если всё пропущено) `{created: [row], skipped: [{table_number, reason: already_exists}]}` — столы с активным `review`-QR пропускаются |
| `GET /api/v1/mobile/branches/{id}/materials/` | — | `{branch: {id, branch_id, name}, links: {mini_app_vk, delivery, site\|null}, qr: {cafe: [{id, name, is_active, url}], delivery: […], delivery_network: […], website: […], review: [{id, name, is_active, table_number, url}]}, print_hint}` — `links.mini_app_vk` и `links.delivery` **без** метки `src` (в воронку не попадают — только переслать гостю, не печатать; об этом `print_hint`), `links.site` = `null`, пока у точки нет активного QR режима `website` (метка `web=<src>` там обязательна); `telegram` в v1.5 нет |

Картинка QR (PNG/SVG) в v1.5 **не отдаётся**: CheckUp рисует по `url` на фронте (веб — qrcode, телефон — svg). Причины: в LoyalUP нет QR-библиотеки (зависимость = пересборка образа, решение владельца), а `<img src>` не носит `Authorization`. Ошибки — `{code, detail}`: `not_found`, `invalid_payload`, `table_required`, `has_scans`. Чужой QR или чужая точка — `404`, не `403`.

### 3б.2. №31 — авторассылки: полный конструктор правил (формы на ревью CheckUp; код после «ок»)

**Что есть.** Правило `AutoBroadcastRule`: событие (`event`, 10 штук — ДР за 7/1 дней и в день, через 3 ч после игры, подарок не забран, не приходил N дней, подписался N дней назад, догоняющее, RF-подарок сгорает, просьба поделиться номером), задержка `delay_days` (пусто = по умолчанию события; у `no_visit_days`/`subscribed_days` обязательна), окно отправки по МСК `send_hour_start/end` (9–21), период `active_from/to`, аудитория = точки (`branches`, пусто = все) + пол + RF-сегменты, текст (лимит ВК 4096) с плейсхолдерами `{имя} {баланс} {награда} {подарок} {дней_осталось} {адреса}`, подарочный шаг (`gift_tier` `G1`/`G1,G2`, `gift_lifetime_days`, `gift_fallback_text`), A/B-варианты с весами, догоняющее правило (`parent_rule` + условие `not_read`/`not_visited`), приоритет. Отправляет beat каждые 15 минут; получатели = резолвер события → общий лог дедупа (ключ менять нельзя, ~13k записей) → недельный кэп сети → RF-оркестратор. Сейчас API умеет только список без пагинации, `PATCH` текста и `is_active`, предпросмотр. Других фильтров аудитории (`first_visit_only`, `min_visits`, `days_since_visit`, `has_phone`) в модели **нет** — кабинет показывает те три, что есть; «задержка» — в днях (`delay_days`), не в минутах: событие «через 3 часа после игры» фиксировано.

**Совместимость.** Пути те же, что у мобильного приложения LoyalUP (`/api/v1/auto-broadcasts/…`), ответы — **надмножество** старых (ключ списка `rules`, старые поля остаются), поэтому список с пагинацией отдаёт `{rules, total, limit, offset}` (без `limit` — полный список, как раньше), а не `results`. `PATCH is_active` остаётся без гейта для мобилки; **CheckUp включает правило только через `activate/`** с `expected_count` (см. 3б.6 «никогда»).

| Ручка | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/auto-broadcasts/events/` | — | `{events: [{code, label, description, dedup: year\|day\|entity, delay_unit: 'days', default_delay_days, delay_required, placeholders: ['{имя}', …]}], gender_filters: [{code, label}], follow_up_conditions: [{code, label}], gift_tiers: [{code, label}]}` — единый источник плейсхолдеров (сейчас четыре расходящихся списка сводятся в один) |
| `GET /api/v1/auto-broadcasts/?limit&offset&event&is_active&q&include_archived` | фильтры; RBAC: правило без точек = сетевое, его видит только пользователь без ограничений по точкам; ограниченный видит правила, чьи точки ⊆ его | `{rules: [rule], total, limit, offset}` |
| `GET /api/v1/auto-broadcasts/{id}/` | — | `rule` (карточка, ниже) · `404 not_found` |
| `POST /api/v1/auto-broadcasts/` | `{name, event, delay_days?, send_hour_start?, send_hour_end?, active_from?, active_to?, priority?, audience: {branch_ids?, gender_filter?, rf_segment_ids?}, message_text, reward?: {gift_tier, gift_lifetime_days, gift_fallback_text}, follow_up?: {parent_rule_id, condition}, variants?: [{name, message_text, weight, is_active}]}` — создаётся **выключенным** | `201 rule` · `400 invalid_payload` / `event_unknown` / `delay_required` / `variant_weights_invalid` / `reward_invalid` · `404 not_found` (точка или сегмент недоступны) |
| `PATCH /api/v1/auto-broadcasts/{id}/` | любые поля карточки: `audience` и `variants` — целиком (замена), `message_text`, `is_active` (для мобилки; CheckUp — только `false`) | `rule` · те же `400` · `409 archived` |
| `DELETE /api/v1/auto-broadcasts/{id}/` | — | `204`: правило **архивируется** (`is_archived=true`, `is_active=false`), физически не удаляется — на нём история отправок; в списке только с `include_archived=1` |
| `GET /api/v1/auto-broadcasts/{id}/preview/` | — | старые `{recipients, due_now, reason, sample_text, sample_names}` **+** `count` (= `recipients`, для `expected_count`), `by_branch: [{branch_id, name, count}]`, `sample_texts: [{variant_id, name, text}]`; `409 {code: preview_failed}` если расчёт упал |
| `POST /api/v1/auto-broadcasts/{id}/activate/` | `{expected_count, confirm: true}` — `expected_count` = `count` из preview; допуск max(5, 10 %) в любую сторону, как у рассылок | `200 rule` · `400 expected_count_required` / `confirm_required` / `audience_empty` (count = 0) · `409 audience_changed {expected, actual}` / `already_active` / `archived` |
| `POST /api/v1/auto-broadcasts/{id}/deactivate/` | — | `200 rule` |
| `GET /api/v1/auto-broadcasts/{id}/log/?limit&offset&status` | лог получателей всех запусков правила | `{total, limit, offset, results: [{sent_at, vk_id, name, variant: {id, name}\|null, status: sent\|failed\|skipped\|pending, read_at, error}]}` — отсев дедупом/кэпом/окном в лог **не попадает** (он происходит до создания получателей), поэтому статусов `skipped_dedup`/`quiet_hours` нет |
| `GET /api/v1/auto-broadcasts/{id}/stats/` | — | `{sent, read, failed, open_rate, sent_30d, last_run_at, variants: [{id, name, weight, is_active, sent, read, failed, open_rate}]}` |
| `POST /api/v1/auto-broadcasts/{id}/variants/` · `PATCH/DELETE …/variants/{vid}/` | `{name, message_text, weight (≥1), is_active}` | `201/200 variant` · `204` · `409 has_sends` при удалении варианта с отправками (тогда `is_active=false`) |
| `POST /api/v1/auto-broadcasts/{id}/test-send/` | `{vk_id}` — гость сети (обычно сам сотрудник) | `{ok, message_id}`: текст рендерится для этого гостя и уходит в ВК **без** записи в лог дедупа и статистику · `400 guest_not_found` / `not_subscribed` · `409 no_vk_token` (у точки гостя нет подключённого сообщества) |

**Карточка `rule`:** `{id, name, event, event_label, is_active, is_archived, priority, delay_days, default_delay_days, send_hour_start, send_hour_end, active_from, active_to, audience: {branch_ids: [внутренние id; [] = все точки], gender_filter, rf_segments: [{id, code, name, emoji}]}, audience_summary, message_text, image: null, reward: {gift_tier, gift_lifetime_days, gift_fallback_text}, reward_summary, follow_up: {parent_rule_id, parent_rule_name, condition} \| null, variants: [{id, name, message_text, weight, is_active, sent, read, failed, open_rate}], stats: {sent, read, failed, open_rate, sent_30d, last_run_at}, created_at, updated_at}` + старые плоские поля мобилки (`branches_count`, `segments_count`, `sent_total`, `sent`, `read`, `failed`, `open_rate`, `parent_rule_name`). Картинка правила в v1.5 не поддержана (как у рассылок). Пожелания CheckUp, которых не будет: `dedup_note` (сколько отсеял дедуп — считается только внутри движка, наружу не выдаётся), `quiet_hours` сети (окно — свойство правила: `send_hour_*`).

**Изменения в модели:** `AutoBroadcastRule.is_archived` (тенантная миграция senler/0014, `db_default=False`, рестарт web и celery сразу после — урок 17.09).

**Сделано 18.09 (код в main, ждёт выкладки вторым раундом с миграцией). Уточнения по коду:**
- `delay_required` — ровно у `no_visit_days` и `subscribed_days` (движок читает `delay_days` без значения по умолчанию только там); у дней рождения и «через 3 ч после игры» задержка не читается вовсе, у остальных есть `default_delay_days`. На `PATCH` проверка задержки идёт только если в теле есть `event` или `delay_days` (мобилка шлёт один `message_text`).
- Пути общие с мобильным приложением, поэтому **RBAC действует и на него**: сотрудник с ограничением по точкам больше не видит сетевые правила (без точек) — списка и карточки (`404`). Пользователи без ограничений не затронуты. Ошибки на старых путях — теперь `{code, detail}` (текст «Правило не найдено» сохранён).
- `DELETE …/variants/{vid}/` с отправками → `409 has_sends` без побочных действий (выключать — `PATCH is_active=false`); при замене `variants` целиком через `PATCH` правила вариант с отправками не удаляется, а выключается (статистика сохраняется).
- `test-send/`: отказ ВК с кодом 901 → `400 not_subscribed`, иной отказ → `502 vk_error {detail}`; троттл `409 rate_limited`; ничего не пишется в лог дедупа и статистику.
- Разархивации нет: `is_archived` через `PATCH` не принимается. `preview/` считает аудиторию одним проходом (те же функции, что `engine.preview_rule`).
- Гейт `use_activate` (★10) — по личности из обмена токена (`CheckUpIdentity`), запасной признак — имя пользователя `checkup-<id>-<schema>`.

### 3б.3. №23 — «Игра через сториз»: одна ручка сети + переопределение точки

Три места настроек: сеть (`ClientConfig.story_*`, 11 полей, только суперадминка), точка (`BranchConfig.story_*`, 5 полей-переопределений, `null`/пусто = как в сети), подарки (товары с признаком «приз сториз», привязанные к точке, + картинка сториз точки). Резолв «точка → сеть → значение по умолчанию» уже есть в коде мини-аппа; сотруднику доступны только три текстовых поля через `PATCH /mobile/branches/{id}/`. Миграций в v1.5 **нет**: шесть полей (минуты активации, «нужен визит», срок и напоминание подарка, даты кампании) остаются только сетевыми — так и фиксируем.

| Ручка | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/settings/story/` | — (`client` — только чтение) | `{settings: {story_game_enabled, story_min_order_amount, story_activation_minutes, story_require_cafe_visit, story_cafe_address, story_activation_text, story_saved_text, story_gift_lifetime_days, story_gift_reminder_days, story_campaign_start, story_campaign_end}, placeholders: ['[адрес кафе]', '[сумма]', '[время]', '[название кафе]', '[название подарка]'], branch_override_fields: ['story_game_enabled', 'story_min_order_amount', 'story_cafe_address', 'story_activation_text', 'story_saved_text'], prizes: {network_count}}` |
| `PATCH /api/v1/settings/story/` | любые поля `settings` (только `network_admin`) | `200` как GET · `400 invalid_payload` (с `editable`) · `403 role_not_allowed` |
| `GET /api/v1/mobile/branches/{id}/story/` | — | `{overrides: {story_game_enabled: true\|false\|null, story_min_order_amount: int\|null, story_cafe_address, story_activation_text, story_saved_text}, effective: {все 11 полей после резолва}, source: {поле: 'branch'\|'network'\|'branch_address'\|'default'}, prizes: {count, first: {id, name}\|null, story_image_url}, rendered: {activation_text, saved_text}}` — `effective` считает **та же функция**, что показывает гостю (общий резолв «точка → сеть → по умолчанию»); `rendered` = тексты с подстановками по первому подарку пула точки (если пул пуст — `[название подарка]` остаётся как есть); `prizes.count = 0` при включённой игре = «включено, но подарков нет»; `story_image_url` абсолютный |
| `PATCH /api/v1/mobile/branches/{id}/story/` | поля `overrides`; `null` / `""` / `0` = наследовать от сети (только `network_admin`; RBAC по точке) | `200` как GET · `400 invalid_payload` · `404 not_found` |

**Правила наследования — по полям, и они разные (так устроен резолв мини-аппа, в v1.5 не меняем):** булевы `story_game_enabled` и `story_require_cafe_visit` наследуются по «задано/не задано» — `false` у точки **перебивает** `true` сети (выключить игру на одной точке можно); `story_min_order_amount`, `story_cafe_address` и оба текста — по «непустое/пустое»: `0` и `""` означают «как в сети». Следствие: **порог «0 ₽» у точки задать нельзя** — это отдельная правка резолва и тенантная миграция, решение владельца. `story_cafe_address` при пустых значениях точки и сети берётся из адреса точки в карточке — `source: 'branch_address'`. `PATCH` сети пишет только 11 полей белым списком (в той же таблице живут брендинг и интеграции — их API не касается); даты кампании — ISO, `start ≤ end`.

**Оценка: 2 дня** (без миграции; с расширением переопределений точки — +1,5 и тенантная миграция, отдельным решением).

### 3б.4. №28 — отчёт по лояльности: комментарии в базе и PDF без `?token=`

Сейчас `GET /api/v1/analytics/report/` отдаёт цифры 11 секций, `ai_summary` всегда пуст; AI-комментарии генерируются `POST …/report/generate-comment/` и живут **в localStorage браузера** (теряются на другом устройстве, в JSON не попадают); PDF — не серверный: страница `?format=pdf` рендерится в браузере (`window.print()` / jsPDF), а в чужом браузере открывается по `?token=<JWT>` — ровно то, чего контракт запрещает.

| Ручка | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/analytics/report/sections/` | — | `{sections: [{num, title, metric_keys}]}` (11 секций; сейчас список захардкожен в вебе и продублирован в JS) |
| `GET /api/v1/analytics/report/comments/?period&start&end&branch_ids` | тот же период/точки, что у отчёта | `{period_key, comments: [{section_num, text, is_ai, author, updated_at}]}` |
| `PUT /api/v1/analytics/report/comments/?…` | `{comments: [{section_num, text, updated_at?}]}` — пишет только присланные секции; пустой `text` удаляет; `updated_at` из `GET` защищает от перезаписи чужой правки | `200` как GET · `400 invalid_payload` · `409 conflict {conflicts: [{section_num, updated_at}]}` |
| `POST /api/v1/analytics/report/comments/generate/?period\|start&end&branch_ids` | `{section_num, section_title?, metrics_json?, draft?, save: true\|false}` — тот же промпт и модель, что у живой `…/report/generate-comment/` (её не меняем — на ней веб и мобилка); `save: true` пишет комментарий с `is_ai` | `{text, saved, comment}`; нет ключа → `503 ai_unavailable`, Claude недоступен → `502 ai_unavailable` |
| `GET /api/v1/analytics/report/?…` | как сейчас | **+** `comments: [{section_num, text, is_ai}]` (аддитивно; `ai_summary` остаётся `""`) |
| `GET /api/v1/analytics/report/print/?period&start&end&branch_ids` | под JWT в заголовке (без `?token=`) | `text/html` — самодостаточная печатная версия: инлайн-стили, ни одного `<script>` (только `onclick="window.print()"`), внешних адресов и картинок нет, комментарии из базы; цифры — тем же кодом, что `GET …/report/` (до единицы совпадают с мобилкой и кабинетом); состав метрик по секциям — таблица представления `SECTION_METRICS` (новая метрика веб-страницы появится в печати, когда её добавят туда — тот же размен, что у JSON-ручки). BFF CheckUp получает страницу сервер-сервер и показывает со своего домена в iframe sandbox. Серверного PDF нет (weasyprint не установлен — отдельное решение владельца) |

**Модель:** `LoyaltyReportComment` (тенантная, новая таблица: период, `branch_ids` + `branch_key` («1,3» или `all`), секция, текст, `is_ai`, автор; уникальность по периоду+`branch_key`+секции; миграция analytics/0007). **Сделано 18.09** (код в main, ждёт выкладки).

### 3б.5. №29 — AI-маркетолог: настройки, лента постов, действия

API нет совсем: есть модели `MarketerSettings` (одна на сеть: включён, автопостинг, дайджест по дням/часу, голос бренда, факты владельца, токен стены ВК — отдельный от токена рассылок) и `MarketerPost` (тип digest/insight/promo/custom, статус draft/published/rejected/failed, текст, снимок фактов, ошибка), задача генерации дайджеста по расписанию, публикация на стену через `wall.post`. Сущность сетевая, точек нет → RBAC по точкам неприменим: `client` — только чтение, `network_admin` — всё.

| Ручка | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/marketer/settings/` | — | `{is_enabled, autopost_enabled, digest_enabled, digest_weekday, digest_hour, last_digest_at, brand_voice, extra_facts, vk_group_id, vk_wall_token_set: bool}` — сам токен не отдаётся |
| `PATCH /api/v1/marketer/settings/` | те же поля **кроме токена стены** (он остаётся в админке до отдельного слова владельца — №55); `vk_wall_token` в теле → `400 invalid_payload` (не игнорируется молча); чужие ключи → `400` | `200` · `400 invalid_payload` · `403 role_not_allowed` |
| `GET /api/v1/marketer/posts/?status&type&limit&offset` · `GET …/{id}/` | — | `{total, limit, offset, results: [{id, post_type, status, text, model_used, created_by, published_at, vk_post_id, vk_post_url, error, created_at, updated_at}]}` |
| `POST /api/v1/marketer/posts/generate/` | `{}` (`network_admin`) | `202 {queued: true, lock_seconds: 300}` — как экшен админки «сгенерировать сейчас»; результат появится в ленте черновиком (или `failed` с ошибкой) · `409 already_generating {retry_after}` в окне 5 минут · `409 marketer_disabled` (выключен маркетолог или дайджест) · `503 queue_unavailable`, если задачу не удалось поставить (блокировка снимается) |
| `PATCH /api/v1/marketer/posts/{id}/` | `{text}` — только у `draft`/`failed`; статус не меняет; `created_by` **не перезаписывается** (признак «писал ИИ» сохраняется; «кто последний правил» — отдельное поле, не в v1.5) | `200 post` · `403 role_not_allowed` · `409 not_editable` |
| `POST /api/v1/marketer/posts/{id}/publish/` | `{confirm: true}` — публикация необратима; конфигурация (`is_enabled`, токен, группа) проверяется **до** вызова публикации — иначе publisher пометил бы пост `failed` из-за настройки | `200 post` (`published` + `vk_post_url`); уже опубликованный → `200 {already_published: true, post}` без повторного вызова · `400 confirm_required` · `403 role_not_allowed` · `409 not_publishable` / `marketer_disabled` / `no_vk_token` · `502 vk_error {detail: текст ВК как есть, post}` |
| `POST /api/v1/marketer/posts/{id}/reject/` | — | `200 post`; уже отклонённый → `200 {already_rejected: true, post}` · `409 not_rejectable` |
| `GET /api/v1/marketer/posts/{id}/context/` | — | `{context: {…снимок фактов, из которых написан пост}}` |

`post` дополнительно несёт `created_by_label` («ИИ» для автогенерации), `vk_post_url` (`https://vk.com/wall-<group>_<id>`), `is_editable`. **Сделано 18.09** (код в main, ждёт выкладки; celery не перезапускается — новых задач нет).

### 3б.7. №52 база знаний ИИ и №56 флаги механик (после 29)

- **№52** `GET /api/v1/ai/knowledge/` → `{documents: [{id, title, is_active, filename, has_text, char_count, created_at, updated_at}]}` · `POST` (multipart `file` `.docx`/`.txt` + `title`) → `201` · `PATCH /{id}/` `{title, is_active}` · `DELETE /{id}/` → `204` · `GET /{id}/text/` → `{text}` («что видит ИИ»). Записывает только `network_admin`. **Сделано 18.09:** список с `limit/offset/total` и `include_archived`; `.pdf` → `400 invalid_payload` с объяснением (текст из PDF не извлекается — ИИ получил бы пустой документ); без `title` берётся имя файла; `DELETE` = архив (`is_active=false`, файл и текст остаются); `/text/` — до 20 000 символов, `truncated`, `char_count`.
- **№56** `GET /api/v1/settings/features/` — **только чтение**: `{flags: {<ключ>: {value, source: 'network'\|'default'}}}` по белому списку (`story_*`, `birthday_window_days`, `auto_broadcast_weekly_cap`, `rf_orchestrator_enabled`, `vk_catalog_enabled`, `vk_catalog_city`, `vk_review_branch_inference`, `vk_review_branch_inference_hours`, `web_entry_enabled`, `degrade_enabled`, `guest_phone_enabled`, `guest_phone_reward_coins`, `code_prompt_message`, `quest_show_message`, `brand_color`, `brand_color_secondary`). Касса и интеграции (`pos_type`, iiko, Dooglys, секреты) **не отдаются никогда**. Гостевая `GET /api/v1/company/<id>/` не меняется. Запись — по слову владельца. **Сделано 18.09:** 26 полей, у ответа `read_only: true`, у вьюхи нет методов записи; `source: default` = значение равно дефолту поля модели.
- **Права по точкам у каталога/квестов/акций** (доделка из 3.2): для пользователя с ограничением по точкам — акции и квесты только своих точек (список, запись), каталог и категории — чтение всё, запись `403 role_not_allowed` (сущности сетевые). **Сделано 18.09 (м34):** включается с выкладкой коммита `a714b7d` в main — дату сообщим; у квестов `all_branches: true` для ограниченного сотрудника означает «все МОИ точки», а не все точки сети; чужой квест/акция → `404 {code: not_found}` (на этих старых ручках — единственный ответ в форме `{code, detail}`). Для пользователей без ограничений поведение мобилки и веба не изменилось.

### 3б.8. Решения по ревью CheckUp 18.09 (нумерация CheckUp; где расходится с таблицами выше — действует этот раздел)

CheckUp принял 3б.1–3б.7 и прислал 11 блокеров и 26 мелочей. Решено:

| № | Решение |
|---|---|
| ★1 | `GET /contact-points/` отдаёт и `totals` (по странице), и `totals_filtered` (по всему фильтру) — оба сразу в v1.5. |
| ★5 | `url` QR и все ссылки «Материалов» собираются из настроек (id мини-аппа, `client_id`, публичный `branch_id`), от `Host`/схемы запроса не зависят — печатать можно. Картинки (`story_image_url`, `image_url` каталога/акций) строятся от домена сети, не от `Host` запроса. |
| ★9 | Роль с ограничением по точкам обязана передавать непустой `audience.branch_ids` ⊆ своих точек в `POST`/`PATCH` правила — иначе `400 invalid_payload`; сетевое правило (`[]`) создаёт только пользователь без ограничений. |
| ★10 | Симметрия гейта: для пользователей из обмена токена (`checkup-*`) `PATCH is_active: true` → `400 use_activate`; мобильное приложение LoyalUP (свои токены) сохраняет старый переключатель. `activate/` — единственный путь включения из кабинета. |
| ★15 | `PATCH …/story/` с `0`/`""` отвечает `200`, в `overrides` это поле `null`, в `source` — `network`; ограничение «порог 0 ₽ невозможен» — в тексте ручки. |
| ★18 | Пул призов сториз управляется из кабинета: у №19 `catalog/products` появляется признак `is_story_prize` (чтение, `POST`, `PATCH`) — вместе с №23; привязка приза к точкам — как у остальных товаров (`assignments`). |
| ★20 | `PUT …/report/comments/` пишет только секции, присутствующие в теле; у каждой можно передать `updated_at` из `GET` — при расхождении `409 conflict {section_num, updated_at}`; пустой `text` удаляет комментарий (м22). |
| ★21 | `period_key` = `<start>_<end>_<внутренние id точек по возрастанию через запятую | all>`, где `start`/`end` — уже разрешённые даты (`period=30` и `start&end` того же диапазона дают один ключ). |
| ★23 | `…/report/print/` — самодостаточный HTML: инлайн-стили, без скриптов, картинки `data:`/абсолютные https; пригоден для `iframe sandbox`. Данные для своей вёрстки — `GET …/report/` + `…/comments/` (JSON уже есть). |
| ★25 | `POST marketer/posts/generate/` → `202 {queued: true}` и блокировка на сеть 5 минут: повторный клик → `409 already_generating`; черновик появляется в ленте по готовности (в модели нет статуса «генерируется» — миграций ради него не делаем). |
| ★35 | Multipart: №19/№21 `image` — jpg/png/webp, потолок 5 МБ (проверка в ручке → `400 invalid_payload`; выше лимита nginx — `413` без JSON); №52 `file` — `.docx`/`.txt`, 10 МБ → `400 invalid_payload`. |
| ★37 | Идемпотентность без заголовка: `activate/` и `publish/` идемпотентны по состоянию (`409 already_active` / `not_publishable`), `test-send/` — не чаще раза в минуту на правило (`409 rate_limited`), `generate/` — блокировка ★25. |
| 2 | ⚠️ `period` — **один ключ в двух смыслах**: у точек контакта, `analytics/stats/`, `report/` и `report/comments/` это пресет-строка `today \| 7d \| 30d \| 90d \| year \| all` (раскрывается в `start`/`end`: 7d = 6 дней назад…сегодня, 30d = 29, 90d = 89, year = с 1 января, all = с 2000-01-01; явные `start&end` перебивают; число вроде `30` → `400 invalid_payload`), а у `GET /mobile/reviews/` — **число дней**; `analytics/reviews/summary/` — `7d\|30d\|all`; 3: `scans{d7,d30,all}` — окна от «сейчас», `funnel`/`totals` — по `meta.start..end`; 4: `conversion` = `subscribed / guests × 100`, целые проценты; 6: `vk_id` строкой; 7: `guest_id` — внутренний id гостя (для склейки), карточка гостя — по `vk_id` (`GET /guests/{vk_id}/`); 8: `to < from` и диапазон > 200 → `400 invalid_payload`. |
| 11 | `409 preview_failed` в preview означает и невозможность `activate/` (тот же расчёт) — кабинет показывает ошибку и не даёт включить. 12: `test-send/` — троттл 1/мин на правило, факт пишется в серверный лог (не в лог дедупа). 13: `next_run_at` — не в v1.5 (beat каждые 15 мин, окно правила задаёт часы). |
| 14 | `source` — по всем 11 полям сториз (у шести сетевых — `network\|default`). 16: `story_min_order_amount = 0` у **сети** тоже означает «по умолчанию» (600 ₽ из кода) — настоящего нуля в v1.5 нет нигде. 17: три текста через `PATCH mobile/branches/{id}/` и `…/story/` пишут один и тот же `BranchConfig`, порядок не важен. 19: переопределения точки пишет только `network_admin` — намеренно, как №15; экран сториз в CheckUp под `loyalty.network`. |
| 22 | Пустой `text` = удалить комментарий. 24: `generate-comment/` с `save: true` — право `manage` и только по действию человека. |
| 26 | `failed` пост: `PATCH text` статус не меняет, `publish/` допустим из `draft` и `failed`. 27: фильтр называется `post_type`. 28: `created_by` — имя пользователя LoyalUP (`full_name` из обмена, иначе `username`), `ai` — у автогенерации. 29: `502 vk_error` — текст ответа ВК как есть в `detail`. |
| 30 | `GET /ai/knowledge/` — с `limit/offset/total`, как все списки. 31: `DELETE` документа = архив (`is_active=false` + скрыт из списка без `include_archived=1`), физически удаляет только админка. 32: `{id}/text/` — до 20 000 символов, `truncated: true` и полный `char_count`. 33: `settings/features/` читают обе роли. 34: права по точкам у каталога/квестов/акций — «включено с коммита X», сообщим датой; до этого CheckUp дублирует гейт. |
| 36 | `{code, detail}` — только у новых ручек волны 2; готовые (каталог, категории, квесты, акции, коды дня, ассистент) остаются с `{"error"}` и английскими 404 DRF (3.2). |

**Решения владельца 18.09 (переданы через CheckUp):** (1) кнопки «тест-отправка», «сгенерировать пост», «опубликовать» живут в кабинете CheckUp под `loyalty.network` с подтверждением и показом текста; LoyalUP даёт троттлы и идемпотентность (★12/25/37) и `502 vk_error` с текстом ВК как есть. (2) Отчёт: CheckUp берёт страницу `…/report/print/` LoyalUP (вид и цвета LoyalUP) под JWT через BFF; условия ★23 в силе — самодостаточный HTML (инлайн-стили, шрифты и картинки `data:`/абсолютные https), без скриптов, ходящих за данными; кнопка печати — только `window.print()`. (3) Пул призов сториз управляется из CheckUp: у №19 `catalog/products` признаки участия в механиках `is_story_prize` и `is_vk_catalog_welcome` (к уже отдаваемым `is_super_prize`, `is_birthday_prize`) — чтение, `POST`, `PATCH`; в `GET …/story/` — `prizes.pool: [{id, name, emoji, price}]` (порядок гостя, до 50 штук; `prizes.count` — полный размер, `prizes.first = pool[0]`) по этому признаку для точки — **сделано 18.09** вместе с №23. `story_image_url` — абсолютный от основного домена сети (`https://<schema>.levelupapp.ru/media/…`), запрос в сборке не участвует.

### 3б.6. Общие правила новых ручек волны 2

- Форма ошибки `{code, detail}`; коды в таблицах выше. Валидация — `400 invalid_payload` (без новых `422`).
- Чужой объект и недоступная точка — `404 not_found` (существование не раскрываем); `403` — только роль.
- Пагинация `limit` (1..200, по умолчанию 50) / `offset` / `total`; фильтры — внутренние `id` точек; в объектах точка как `{id, branch_id, name}`.
- Права по точкам — через `effective_branch_ids` (как `broadcasts/*`); `feature_access` API не проверяет — гейтит CheckUp (2.2).
- Всё, что меняет деньги, призы или массовые отправки, — `expected_count` + `confirm: true` и `409` при расхождении (правило 3.1).
- Каждая новая ручка приходит с тестами на моках и эталонами в `fixtures/w2/`; `@extend_schema` — сразу, чтобы срез схемы был честным.

**«Никогда» волны 2 (дополнение к разделу 4):** включать правило авторассылки через `PATCH is_active=true` (только `activate/` с `expected_count`); записывать токены ВК (`vk_wall_token`, токены сообществ) через API; звать `assistant/ask/`, `report/generate-comment/`, `marketer/posts/generate/` автоматически или по расписанию — только по действию человека; удалять правила, варианты и документы физически там, где контракт говорит «архив»; открывать страницы LoyalUP в браузере сотрудника по `?token=`.

---

## 4. Список «никогда» для стороны CheckUp

Каждый пункт — след реального инцидента или устройства LoyalUP, которого снаружи не видно.

1. **Никогда не ходить в базу LoyalUP** и не читать её схемы напрямую. Только HTTP-ручки из раздела 3.
2. **Никогда не перебирать сети.** Тенант — это `Host`. Нет запросов «по всем тенантам», нет кэшей, общих для двух сетей, нет id, пережившего смену `Host`.
3. **`expected_count` обязателен на любой отправке** (рассылка, кампания). Берётся из ответа предпросмотра, который **показали пользователю**. Пришёл `409` — показать новый охват и спросить снова. Инцидент 21.08.2026: «рассылка на 1 гостя ушла 612» — предохранитель родился из него.
4. **Не доливать отзывы LoyalUP в `GuestReview` CheckUp.** Аналитика NPS CheckUp считается по своим оценочным отзывам; поток LoyalUP — отдельный и основной. Единый пункт меню «Отзывы» показывает поток LoyalUP; пустую ленту CheckUp — под флагом.
5. **Точки — только через `LoyalupBranchMap`.** Никакого матчинга по адресу, названию или телефону. Точка LoyalUP ≠ филиал CheckUp.
6. **Не писать гостям напрямую** (ВК, СМС, пуш) из CheckUp. Только через ручки LoyalUP: лимит ВК 20 сообщений/с на токен сообщества держит LoyalUP, и спам-флаг ВК роняет не рассылку, а отзывы всей сети.
7. **Токен LoyalUP не покидает BFF.** В браузер, в localStorage, в логи — никогда. TTL 60 минут, потом новый обмен.
8. **Только `/api/v1/…`.** HTML-ручки веб-кабинета (`/analytics/…` без `/api/`) и админка (`/admin/…`, `/superadmin/…`) — не для машин: у них сессия, CSRF и другая модель прав.
9. **Гостевые ручки мини-аппа не для CheckUp:** `/api/v1/client/…`, `/testimonials/`, `/vk/…`, `/code/`, `/branches/{branch_id}/`. Они защищены подписью запуска ВК и сессией гостя, сотрудник туда не ходит. Не просить включать `VK_SIGN_ENFORCE`.
10. **Не повторять автоматически `POST`/`PATCH`/`DELETE`.** `adjust-coins` при повторе создаст вторую транзакцию, `reply` — второе сообщение гостю. Повтор — только `GET`, и только один раз после обновления токена.
11. **Не трогать состояние сети:** `paid_until`, `is_active`, флаги `ClientConfig`, пороги RF, каталог наград. Это оператор платформы и волна 2.
12. **Не тянуть «всё» циклами** там, где пагинации нет (уведомления до её появления, сообщения треда). Показывать первые N и честно писать «показаны последние N».
13. **`branch_id` из QR-ссылок и жалоб — не то же самое, что `id` точки.** В фильтры ручек раздела 3 передавать `id`; в `branch_ids` обмена токена и в `point_id` жалоб ходит публичный `branch_id` — это единственный идентификатор точки, который знает `LoyalupBranchMap`.
14. **Не строить второй чат, второй колокольчик, второго ассистента.** Саппорт-чат уже слит релеем, уведомления идут в колокольчик CheckUp (5.3), «Лояльчик» вливается в раздел AI CheckUp в волне 2.
15. **Секреты не переиспользовать.** `LOYALUP_RELAY_SECRET` — жалобы и чат; `CHECKUP_TOKEN_EXCHANGE_SECRET` — только обмен. Утечка одного не должна открывать другое.
16. **Никогда не делать обмен с `role: "client"` по id живого сотрудника ради проверки** (v1.4). Обмен перезаписывает роль и точки по телу, и уже выданный JWT у BFF тут же становится «клиентским». Проверки — только синтетическими id (`fx-…`, `test-…`); смена роли или точек в CheckUp → сброс кэша токена по `(tenant_schema, checkup_user_id)` и новый обмен.

---

## 5. Каналы «CheckUp ← LoyalUP» и обратно

### 5.1. Жалобы (уже работает)

Негативный отзыв или низкая оценка из LoyalUP уходит в реестр жалоб CheckUp: `POST https://checkupapp.ru/api/v1/loyalup/complaints/inbound/`, заголовок `X-LoyalUP-Relay-Secret`. Тело:

```json
{ "tenant_schema": "levone", "complaint_id": "3224-app", "text": "…", "created_at": "…",
  "point_id": "2", "point_name": "Levone | Набережная", "address": "…",
  "rating": 2, "guest_name": "…", "guest_phone": "+7…", "guest_vk_id": 123456789,
  "photos": ["https://…"],
  "point_inferred": false, "point_inferred_source": "", "point_inferred_scan_at": null,
  "table_number": 7 }
```

Дедуп на стороне CheckUp по `loyalup:<tenant>:<complaint_id>` и 6-часовому окну. `point_id` — публичный номер точки. С 16.09 точка берётся у самого сообщения (столы на точках повторяются), фолбэк — точка переписки. Жалоба без точки не отправляется (`CHECKUP_COMPLAINTS_RELAY_UNPOINTED` выключен).

`guest_vk_id` (v1.2) — публичный числовой VK ID гостя: профиль в точке → карточка ВК → отправитель треда, первый сложившийся. Тип — целое число или `null`; строку и отрицательный ID (группа-автор, а не гость) LoyalUP не присылает, а кладёт `null`. Ссылка на гостя — `https://vk.com/id<guest_vk_id>`; собирает её CheckUp, отдельного `guest_vk_url` в payload нет. Поле необязательное: жалоба уходит и без ID, просто без связи с гостем.

### 5.2. Вердикт по жалобе (ручка LoyalUP, есть с 16.09)

До этой ручки обратного канала не было: вердикт, ответственный и статус жалобы в LoyalUP не возвращались, и в ленте отзывов LoyalUP жалоба выглядела нерешённой. Теперь:

```
POST http://127.0.0.1:7000/api/v1/internal/complaints/verdict/
Host: levelupapp.ru                      (или Host: <schema>.levelupapp.ru — тогда tenant_schema в теле не нужен)
X-LoyalUP-Relay-Secret: <LOYALUP_RELAY_SECRET>

{ "tenant_schema": "levone", "complaint_id": "3224-app",
  "status": "resolved" | "rejected" | "in_progress",
  "verdict": "Компенсация 500 баллов, официант предупреждён", "manager_name": "Алина",
  "resolved_at": "2026-09-16T18:00:00+03:00", "force": false }
```

Ответ `200 {"ok": true, "tenant_schema": "levone", "conversation_id": 3224, "status": "resolved", "previous_status": ""}`.

- `complaint_id` — ровно тот, что пришёл в жалобе (`<id переписки>-<корзина>`); LoyalUP берёт из него id переписки. `resolved_at` необязателен: для `resolved`/`rejected` без него ставится время получения; для `in_progress` дата закрытия очищается.
- LoyalUP **пишет вердикт в служебные поля карточки отзыва** (`checkup_status`, `checkup_verdict`, `checkup_manager`, `checkup_resolved_at` — они же в `GET /api/v1/mobile/reviews/`) и показывает плашку «CheckUp: Решено · Алина · дата — текст» в веб-кабинете и мобилке. **Сообщение гостю и строка в треде не создаются** — ответ гостю идёт обычной ручкой `reply` под токеном сотрудника, чтобы автор ответа был известен и работали пуши/автоответы как всегда.
- Ошибки: `400 invalid_json` / `invalid_complaint_id` · `403 forbidden` (внешний адрес или неверный секрет) · `404 tenant_not_found` / `complaint_not_found` (переписки с таким id в этой сети нет) · `409 already_closed {"current_status": "rejected"}` — жалоба уже закрыта с другим статусом; переписать можно только с `"force": true` · `422 invalid_status` / `invalid_payload` · `500 not_configured`. Секрет тот же, что у жалоб (`LOYALUP_RELAY_SECRET`): вердикт — часть канала жалоб.

### 5.3. Уведомления сотруднику

События LoyalUP (новый негатив, черновик ИИ готов, автоответ через N минут, рассылка отправлена, сигнал мониторинга) в волне 1 читаются BFF из `GET /api/v1/notifications/` и раскладываются в колокольчик CheckUp по `type`. Пуш на телефон сотрудника CheckUp остаётся за CheckUp. Веб-хук «событие → CheckUp» — волна 2, когда будет понятно, каких событий не хватает.

### 5.4. Гость и телефон (контракт «гость»)

Сущность гостя у двух систем разная: в LoyalUP гость — это `vk_id`, в CheckUp — телефон. Склейка: **по нормализованному телефону (E.164), когда он есть у обеих сторон; иначе стороны не склеиваются.** Сегодня телефон в LoyalUP есть только у отзывов из мини-аппа (`TestimonialMessage.phone`), поэтому покрытие частичное. №78 добавляет телефон гостю с его согласия через ВК (`VKWebAppGetPhoneNumber`, подпись «защищённым ключом» приложения) — после него у гостей, давших номер, склейка полная. Дедуп между почтовыми жалобами Автосуши и жалобами LoyalUP делается на стороне CheckUp по этому же правилу.

---

## 6. Данные, которые CheckUp должен знать «как есть»

| Сущность | Что важно |
|---|---|
| Сеть (`Company`) | `schema_name` = поддомен; `client_id` — публичный номер сети из QR-ссылок; `is_active`, `paid_until` — оператор платформы. Неактивная или неоплаченная сеть отвечает `404`/`403` на обмен токена |
| Точка (`Branch`) | `id` — для фильтров API и прав внутри LoyalUP; `branch_id` — публичный: QR, ссылки, `point_id` жалоб, `branch_ids` обмена токена; `is_active` |
| Сотрудник (`User`) | роли `superadmin` / `network_admin` / `client`; `branch_access` проверяется API; `feature_access` (18 ключей) — нет |
| Переписка (`TestimonialConversation`) | одна на пару «точка + гость»; тред из ВК-группы без точки (`branch = null`), у него может быть подсказка `inferred_*` по последнему скану; с 16.09 у каждого сообщения своя точка и стол |
| RF-оценка (`GuestRFScore`) | привязана к гостю сети (`vk_id`), не к профилю точки; пороги 14/30/60 дней утверждены владельцем |
| Рассылка | аудитория = ровно показанная ячейка (сегмент, точки, период); отправка серийная, ≤20 сообщений/с; отмена работает на идущей |
| Авторассылки | общий дедуп-лог для правил и legacy-шаблонов: одно событие → одно сообщение гостю. Свой дедуп на стороне CheckUp = дубли тысячам гостей — не делать |

---

## 7. Песочница

- **Сеть:** `dev` → `https://dev.levelupapp.ru` (в контуре сервера: `Host: dev.levelupapp.ru`). Сеть и домен есть, сертификат покрывает; до 16.09 в ней была одна точка (`branch_id 239014483`, «Конференция Автосуши») и ни одного пользователя — отсюда `404` на `branches/1/`. Команда LoyalUP `manage.py seed_checkup_sandbox --commit` добавляет вторую точку `branch_id 990002` («Песочница CheckUp · Точка 2»), десять тихих тестовых отзывов на обе точки (с ответами и без, разброс по 14 дням) и учётку; повторный запуск ничего не дублирует.
- **Учётка веб-кабинета:** `checkup-sandbox` (роль `network_admin`, сеть `dev`) — для сверки цифр в кабинете `https://dev.levelupapp.ru/admin/`; пароль владелец получает отдельно от документа. Для самого стыка пароль не нужен: BFF делает обмен с любым `checkup_user_id`, и LoyalUP заводит `checkup-<id>-dev` сам.
- **Проверка стыка за минуту:** (1) `GET /api/v1/branches/239014483/` с `Host: dev.levelupapp.ru` → `200`; (2) обмен токена с `tenant_schema: "dev"`, `role: "client"`, `branch_ids: [990002]` → `200`, `tenant_domain = "dev.levelupapp.ru"`; (3) `GET /api/v1/mobile/reviews/` с полученным JWT и `Host: dev.levelupapp.ru` → `200` и только отзывы второй точки; (4) тот же обмен с `role: "network_admin"` → в списке отзывы обеих точек; (5) `POST /api/v1/analytics/rf/send-broadcast/` с `expected_count: 1` на аудиторию из нескольких гостей → `409` — предохранитель на месте.
- **Ответ на тестовый отзыв уходит в ВК даже на `dev`:** `POST …/reply/` зовёт API ВК для любого треда с `vk_sender_id`, включая тестовые `990000001+`; ВК отвечает ошибкой «Can't send messages for users without permission», ответ сохраняется локально (`delivered_to_vk: false`, `vk_error`), до людей ничего не доходит. Замечание CheckUp 18.09: для тестовых гостей API ВК лучше не звать вовсе — кандидат в волну 2 (признак «песочница» у треда, не порог по номеру).
- **Что нельзя даже на `dev`:** токен сообщества ВК настоящий (конференция Автосуши), гостей в сети четверо — рассылки на `dev` только после согласования списка с LoyalUP. Тестовые отзывы помечены `[песочница CheckUp]` и гостями с `vk_sender_id` от `990000001`.

---

## 8. Что делает каждая сторона

### LoyalUP (сторона API и прода)

| Работа | Оценка |
|---|---|
| Обмен токена `internal/auth/exchange/` + таблица соответствия + роли/точки + тесты — **сделано 16.09** (ждёт выкатки на `dev`) | 2 дня |
| Вердикт `internal/complaints/verdict/` + статус в карточке отзыва (веб, мобилка, API) — **сделано 16.09** | 1 день |
| Телефон гостя №78: гостевая ручка с проверкой подписи ВК, поля в карточке гостя, мини-апп — **сделано 17.09** (бэк 7898572…0d2849a, мини-апп собран, пилот dev + LevOne, политика конфиденциальности дополнена разделом о гостях) | 2 дня (бэк) + 1,5 (мини-апп) |
| Пробелы API волны 1: ~~фильтры и пагинация отзывов (1), сводка тональностей и детализация метрик (1), дашборд дня (1), рассылки JSON-CRUD + отправка с `expected_count` (3), аварийные действия (1), точки `PATCH` (2), уведомления курсор (0,5) — всё сделано 16.09~~ | 9,5 дней |
| OpenAPI-срез волны 1 + TypeScript-типы для BFF + пакет записанных ответов песочницы для контрактных тестов CheckUp — **сделано 16.09**: `docs/platform/openapi_w1_2026-09-18.json` (перегенерирован 18.09: + `broadcasts/*`, карточка отзыва, объектные ответы ленты/сообщений, query-параметры), `loyalup_w1_2026-09-18.d.ts`, `fixtures/w1/` (69 записей с обезличенными гостями, README) | 1 день |
| Песочница `dev`: вторая точка, учётка, тихие тестовые отзывы (`seed_checkup_sandbox`) — **сделано 16.09** | 0,5 дня |
| Фикстуры рассылок `broadcasts_*` и перезапись `guest_card` с телефоном; контракт v1.4 (правило client-id, раздел 3.2) — **сделано 18.09** | 0,5 дня |
| **Волна 2 (v1.5, старт 18.09):** эталоны w2 (45 записей) — **сделано 18.09**; №26/27 точки контакта и материалы — в работе; №31 авторассылки (4–5), №28 отчёт (3), №23 сториз (2), №29 маркетолог (2,5), №52 база знаний (1–1,5), №56 флаги (чтение, 1), ограничение по точкам для `client` в каталоге/квестах/акциях (0,5) | ≈ 17 дней после разведки 18.09 (карта считала 15: №53 закрыт, но №31 вырос до 4–5 за счёт активации с гейтом, лога, вариантов и тест-отправки; №28 — 3 с комментариями в базе) |
| **Итого** | **≈ 17 дней** волна 1 + **≈ 17 дней** волна 2 |

Порядок: обмен токена и песочница первыми (без них CheckUp не может начать), затем вердикт и фильтры отзывов (первый экран), остальное параллельно экранам CheckUp.

### CheckUp (сторона кабинета)

- BFF в Django: обмен токена, кэш токена в Redis с TTL ≤ `expires_in`, прокси ручек раздела 3 с маппингом ошибок из 3.1, вывод роли `network_admin`/`client` и `branch_ids` из кодов `loyalty.*` и `LoyalupBranchMap`.
- Экраны волны 1 (12 веб-роутов, 5 мобильных экранов, 7 виджетов) по целевой карте модуля из «Карты переезда».
- `FeatureCode.loyalty` в тарифах — сейчас модуль нечем включить; кабинет «только LoyalUP» (клиенты без остального CheckUp).
- Клиент вердикта (5.2), раскладка уведомлений в колокольчик (5.3), дедуп гостей по телефону (5.4).
- **Волна 0 CheckUp, п. 7:** спящую ручку `POST /api/v1/guests/webhook/levelup/` — **удалить**, не чинить (при пустом секрете всегда `401`, филиал ищет без клиента, в базе три строки за один тестовый день; релей жалоб заменил её полностью).
- **Волна 0 CheckUp, новый пункт:** аналитика отзывов CheckUp уже течёт — жалобы LoyalUP лежат в `GuestReview` (источник `LOYALUP`, с 01.09) рядом с `EMAIL`, а дашборд и аналитика считают средний рейтинг по всем без фильтра источника. Раздел 4.4 требует потоки не смешивать — фильтр по источнику ставится до стыковки, это важнее спящей ручки.
- Контрактные тесты своей стороны (раздел 9).

---

## 9. Приёмка волны 1

Критерии одни для всех экранов: **цифры совпадают с веб-кабинетом LoyalUP за тот же период и те же точки**; **сотрудник одной точки не видит другую** (проверяется учёткой `client` с одной точкой); **любая ошибка LoyalUP показана человеку словами, а не пустым экраном**; **токен LoyalUP не виден в браузере** (DevTools → Network/Storage).

| Роут CheckUp | Возможности | Особая проверка |
|---|---|---|
| `/loyalty` | 4 (дашборд дня), 32 (менеджер) | «негатив без ответа» = число неотвеченных NEGATIVE/PARTIALLY_NEGATIVE в LoyalUP |
| `/loyalty/reviews` | 1, 2 | фильтр по тональности и точке; ответ уходит гостю в ВК и виден в LoyalUP как ответ администратора с именем сотрудника |
| `/loyalty/reviews/[id]` | 1 | у каждого сообщения точка и стол; кнопка отмены автоответа работает в окне |
| `/loyalty/reviews/settings` | 3 | выключение `auto_send_enabled` останавливает запланированные автоответы (`skipped`) |
| `/loyalty/stats` | 5 | клик по метрике открывает тот же список гостей, что `/analytics/stats/detail/` LoyalUP |
| `/loyalty/guests` | 6 | поиск по имени и VK ID; пагинация |
| `/loyalty/guests/[id]` | 7, 8 | корректировка баллов без причины → `400`; после — баланс и транзакция видны в LoyalUP |
| `/loyalty/rf` | 9, 10 | матрица 4×3 совпадает с LoyalUP; пересчёт — спиннер, не двойной запуск |
| `/loyalty/broadcasts` | 11, 12, 13 | предпросмотр → отправка с `expected_count`; смена аудитории → `409` и повторное подтверждение; отмена идущей; аварийные действия требуют `confirm` |
| `/loyalty/campaigns` | 14 | создание с контрольной группой; отмена откатывает подарки |
| `/loyalty/points` | 15 | правка контактов точки видна гостю в мини-аппе |
| `/loyalty/access` | 16 | только чтение «родных» сотрудников LoyalUP; пользователи `checkup-*` в списке помечены |

Приёмку проводит сторона LoyalUP на `dev`, затем на пилоте LevOne с включённым флагом только у этой сети. Клиенты: LevOne → Автосуши → Шавуха → «только LoyalUP».

**Ход приёмки LoyalUP (v1.4, 18.09 00:20–01:00 MSK).** Сторона CheckUp сняла экраны LevOne под владельцем (`network_admin`, все точки, Playwright с запретом на любые записи) и JSON тех же ручек через свой BFF (00:26 MSK); LoyalUP сверил экран ↔ ответ API и API ↔ веб-кабинет (по коду: страницы кабинета `analytics/views.py` считают теми же функциями `get_general_stats`, `get_chart_data`, `get_rf_stats`, `get_migration_history` из `analytics/api/services.py`, что и ручки раздела 3).

| Экран CheckUp (LevOne) | Цифры на экране | Ответ LoyalUP (`dashboard/today`, `analytics/stats`, `reviews/summary?period=30d`) | Итог |
|---|---|---|---|
| `/loyalty` «Сегодня» | негатив без ответа 29 · ждут ответа 249 · черновики ИИ 40 · автоответы 0 · новые 0 · жалобы 0; коды дня 2/2 без кода; оплачено до 22.12.2030 (1556 дн.); точки за 30 дней: Ленина 118/6/0/5.0, Набережная 29/1/0/4.0 | `unanswered_negative 29`, `waiting_reply 249`, `drafts_ready 40`, `auto_send_scheduled 0`, `new_today 0`, `checkup_in_progress 0`; `branches_active 2`, `branches_without_code 2`; `paid_until 2030-12-22`, `days_left 1556`; `top_branches` 118/6/0/5.0 и 29/1/0/4.0 | ✅ совпало полностью |
| `/loyalty/stats` «Месяц» (2026-08-20 — 2026-09-18) | сканы 1 159 · QR 560 · кафе 1 159 · доставка 0 · до игры 640 · повторно 40; гости: оцифрованные 273, в базе 1 762, сообщество 224, рассылка 233, новые с подарком 256; источники сообщества 215/0/9/0/0, рассылки 223/0/10/0/0 | `total_scans 1159`, `qr_scans 560`, `cafe_scans 1159`, `delivery_scans 0`, `game_reached 640`, `repeat_game_players 40`; `unique_digitized_guests 273`, `total_vk_subscribers 1762`, `new_community_subscribers 224`, `new_newsletter_subscribers 233`, `new_group_with_gift 256`; `community_subs_* 215/0/9/0/0`, `newsletter_subs_* 223/0/10/0/0` | ✅ совпало полностью |
| `/loyalty/reviews` «Месяц» | «Показано 1–20 из 1035»; карточки: тональность, оценка, гость, точка/«ВК группа», метки «не отвечен», «непрочитано», «черновик ИИ», «ВКонтакте» | список `mobile/reviews/?period=30` — `total` по тредам с сообщением за 30 дней; сводка `reviews/summary?period=30d` — `total 1033` по отзывам | ✅ форма верна; 1035 ≠ 1033 — **две ручки считают разное** (треды vs отзывы), не ошибка CheckUp; на экране рядом со счётчиком писать «тредов» |
| `/loyalty/rf` «Месяц», окно 30 дней, «Ресторан» | матрица 4×3, всего 545; R3F1 213, R3F2 22 …; пороги 14/30/60 дней и 3/5 визитов подписаны под матрицей | `analytics/rf/` (та же функция `get_rf_stats`, что у страницы кабинета) | ✅ по форме и порогам; сверка чисел с кабинетом — при следующем окне владельца (чтение прод-данных LevOne агентом LoyalUP заблокировано классификатором 18.09) |
| `/loyalty/guests`, `/loyalty/guests/{vk_id}`, `/loyalty/broadcasts`, `/loyalty/campaigns`, `/loyalty/points`, `/loyalty/reviews/settings` | открываются, ошибок страниц 0 (11 веб-маршрутов + 5 экранов телефона) | ручки 6–8, 11–15, 3 | ✅ открываются; числа не сверялись |

**Живая проверка выкладки 461689d (CheckUp, `dev`, 18.09 00:40–00:42, обмен синтетическим id 777001):** обмен `client [990002]` → `200`, `branches [{branch_id 990002, id 3}]`; `analytics/branches/` → `[{id 2, branch_id 239014483}, {id 3, branch_id 990002}]`; список под `client` — только точка 3 (6 тредов), под `network_admin` — 12 тредов двух точек; `mobile/reviews/13/` (своя точка) → `200` той же формы, `/12/` (чужая) → `404`, `/99999999/` → `404`. Тело `404` было английским текстом DRF — с 18.09 01:10 `{"detail": "Отзыв не найден"}`.

**Что не закрыто и почему:** (1) ~~«сотрудник одной точки не видит другую» через кабинет CheckUp~~ — **закрыто 18.09 01:03 (по слову владельца, сторона CheckUp):** в ОФИСе (`dev`) заведён пользователь CheckUp #517 «Приёмка LoyalUP (dev)» с ролью #120 «LoyalUP приёмка (dev, одна точка)» — единственное право `loyalty.view`, доступ по данным только к «Бухгалтерии» (branch 32 ↔ `990002`); вход — https://checkupapp.ru, организация ОФИС, логин по телефону `+70000000090`, пароль у владельца (файл на проде CheckUp). Через BFF под ней: обмен → `client`, `branch_ids [990002]`; `mobile/reviews/?period=all` → 6 тредов, все точки 3 (точка 2 не видна); `/mobile/reviews/13/` → `200`, `/12/` → `404 «Отзыв не найден»`; `POST …/reply/` → `403` «нужно loyalty.manage»; `PATCH mobile/branches/3/` → `403`. До этого — по прямому обмену (§7) и фикстурам `*_client_scoped`, `*_404_foreign`. (2) Пишущие проверки на `dev` **сделаны стороной CheckUp 18.09 01:04 (BFF, владелец как `network_admin`):** `POST mobile/reviews/13/reply/` → `201`, сообщение #18 `ADMIN_REPLY` в треде; `POST …/13/resolve/` → `200`, после — `is_replied true`, `has_unread false`; `403` для роли `client` на `reply` и `PATCH` точки — учёткой #517. **Не сделано — баллы гостю (`adjust-coins`):** в песочнице нет синтетического гостя, все четверо — настоящие люди; `seed_checkup_sandbox` заводит только треды. Решение владельца: заводить ли гостя-песочницу — таблица гостей общая на все сети, и «синтетический» `vk_id` вроде `990000010` может совпасть с настоящим пользователем ВК (номера ВК уже близко к миллиарду), поэтому либо резервный диапазон вне номеров ВК, либо проверка баллов на LevOne по слову владельца. На LevOne пишущих проверок (ответ гостю в ВК, баллы, рассылка, кампания, правка точки) не делали — по слову владельца на каждую, в его окне. (3) ~~«Токен не виден в браузере»~~ — **закрыто 18.09 (Playwright под владельцем, LevOne: `/loyalty`, `/loyalty/reviews`, `/loyalty/guests`, карточка 2937):** браузер ходил только на `checkupapp.ru`, VK ID SDK со страницы входа (`id.vk.ru`, `static.vk.ru`, `api.vk.ru`) и `unpkg.com`; ни одного обращения к `*.levelupapp.ru`; в 11 ответах `/api/v1/loyalty/*` нет JWT LoyalUP; `localStorage` — только `access_token` CheckUp и служебные ключи (`active_tenant_id`, `loyalty_tenant_v1`, `device_id`, `cu_nav_lastused`), `sessionStorage` пуст, cookies только `vk.ru`. Токен LoyalUP живёт в Redis BFF. (4) ~~Карточка отзыва `/loyalty/reviews/{id}` на LevOne~~ — **закрыто 18.09 01:15:** CheckUp подключил карточку к `GET /mobile/reviews/{id}/` (их коммит d252d777, веб выложен, смоук 12/12; телефон — OTA по слову владельца); снимок треда 2937 на LevOne под владельцем: шапка (тональность, гость, «ВК группа», дата), кнопки «Решено / Перегенерировать / Отклонить черновик», лента сообщений с цитатами, черновик ИИ в поле ответа, подпись «ответ уходит гостю в личные сообщения»; путь запросов `loyalty/tenants/ → …/mobile/reviews/2937/ → …/messages/`, ошибок 0. Точка и стол у сообщений на этом снимке не видны — тред из ВК-группы без точки (`branch = null`), проверить на треде из мини-аппа при пишущих проверках (п. 2).

**Контрактные тесты вместо ревью кода.** Односторонний ревью агентами не масштабируется, тесты — да. LoyalUP публикует OpenAPI-срез волны 1 и пакет записанных ответов песочницы (JSON по роутам приёмки, включая ошибки 401/403/409/422) — с 16.09 лежат в репозитории LoyalUP: `backend/docs/platform/openapi_w1_2026-09-18.json`, `loyalup_w1_2026-09-18.d.ts`, `fixtures/w1/*.json` (см. README там же); CheckUp гоняет свой BFF против этих записей в CI. LoyalUP держит у себя тесты формы ответов тех же ручек — изменение формы ломает тест раньше, чем сломает CheckUp. Правило: форма ответа меняется только аддитивно, удаление поля — новая версия контракта.

---

## 10. Открытые вопросы (решает владелец)

1. ~~Пользователь `checkup-<id>` вместо склейки по email~~ — **согласовано обеими сторонами** (склейка по email отдала бы чужому сотруднику чужие права). Уточнение v1.1: личность на пару (сотрудник, сеть).
2. ~~Показывать ли кассирам~~ — **снято**: доступ решает код права `loyalty.*` в CheckUp, а не роль; контракт кассиров отдельно не выделяет.
3. Кто и когда отдаёт «Защищённый ключ» мини-аппа 53418653 — без него №78 (телефон гостя) и проверка подписи ВК не включаются. **Закрыто 16.09: ключ получен и установлен, №78 сделан 17.09.** Осталось: строгий режим подписи запуска (`VK_SIGN_ENFORCE`) — после разбора запросов мини-аппа без заголовка.
4. Ключи Anthropic у двух продуктов **уже разные**; кончались одновременно (31.08, 05–08.09) — значит, общий счёт. Решение: лимиты на счёт или второй счёт. **Открыто, за владельцем.**
5. ~~Когда включать обмен токена для живых сетей после `dev`~~ — **закрыто 17.09 22:45:** LevOne открыт по слову владельца (`CHECKUP_TOKEN_EXCHANGE_TENANTS=dev,levone`); Автосуши и Шавуха — по отдельному слову владельца.

---

*Документ живёт в `backend/docs/platform/PLATFORM_CONTRACT_v1_2026-09-16.md`; артефакт — копия. Изменения — только через сторону LoyalUP, с пометкой версии. v1 — 16.09.2026; v1.1 — 16.09.2026 вечер, по ревью агента CheckUp; v1.2 — 16.09.2026, `guest_vk_id` в жалобе по запросу агента CheckUp; v1.3 — 17.09.2026, №78 телефон гостя сделан и включён на пилоте; v1.4 — 18.09.2026, обмен открыт LevOne, правило «проверки client только чужим id» (риск нашёл CheckUp), шероховатости 3.2, фикстуры `broadcasts_*` и `guest_card` с телефоном.*
