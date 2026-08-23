# Инвентаризация перед этапом 1 трека «Антихрупкость входа» — 2026-08-23 15:45 МСК

Эталонное состояние «как сейчас». Приёмка каждого этапа сверяется с этим документом:
**при всех выключенных флагах прод неотличим от описанного здесь.**

## Прод-коммиты (эталон)

| Репозиторий | Прод-путь | HEAD | Dirty |
|---|---|---|---|
| levelup-back (бэкенд) | /home/levone/levelup-back | `cc616cf` fix(cors): x-vk-launch-params | чисто |
| levone-front-v3 (vk-miniapp, основной бандл) | /home/levone/levone-front-v3 | `3a513d1` (git = слепок; истина в GitHub, деплой rsync) | 48 файлов dirty — НОРМА (см. память deploy-vk-miniapp) |
| levone-front-web (форк, loyalupp.ru) | /home/levone/levone-front-web | `f14586a` Added offline promotion | 2 файла |

Локальный vk-miniapp HEAD на этот момент: `50fdf0f` (X-VK-Launch-Params + анти-DDoS) — собран и раздаётся с levonework.ru.

## Бэкапы (сделаны сегодня)

- `/home/levone/levone-front-v3/build.bak-20260823-1544/` — прод-сборка основного бандла (включая banquet).
- `/home/levone/backups-front-web-20260823.tar.gz` (16M) — форк loyalupp.ru целиком (src+build, без node_modules).
- `/opt/checkup/backup_loyalup_pre_stage1_20260823.sql.gz` (23M) — полный pg_dump.
- Рядом лежит `/opt/checkup/backup_loyalup_full_20260823.sql.gz` — утренний бэкап CheckUp-сессии (до фиксов vk_id).

## Домены → что раздают (из nginx, конфиги в docs/nginx/)

| Домен | Root/прокси | Роль |
|---|---|---|
| levelupapp.ru + *.levelupapp.ru | Django (gunicorn) | Бэкенд API + тенант-поддомены + админки |
| levonework.ru | статика /home/levone/levone-front-v3/build | Основной мини-апп (ВК-iframe) + /banquet/ (посторонний сайт!) |
| vkapp.levelupapp.ru | статика /home/levone/levone-front-v3/build | Тот же бандл (⚠️ isVk-проверка App.jsx его НЕ знает) |
| loyalupp.ru | статика /home/levone/levone-front-web/build | Форк (мёртвый: трафик = боты); в конфиге ЕСТЬ прокси /vkid → id.vk.ru под OAuth |

Остальные домены на сервере (checkupapp, levone-cafe, levonereviews, shavuha*, starsdigital, tunnel) — другие проекты, НЕ трогаем.

## Состояние флагов/механизмов на момент эталона

- `VK_SIGN_ENFORCE=off` (переходный режим, счётчик в лог) — включение on планово через 2-3 дня.
- AI-маркетолог: включён ТОЛЬКО на levone, режим черновиков (autopost выкл); дайджест id=2 ждёт публикации.
- RF-пилот levone: 9 правил активны, оркестратор вкл.
- Механизмов трека 2г НЕ СУЩЕСТВУЕТ: нет /go, нет веб-входа, нет флагов, нет Domain-записи для levonework.ru, деградации нет (вне ВК — заглушка NotVk / тупик vk_data_invalid).

## Куда едет этап 1 (для сверки после)

Всё новое — ТОЛЬКО под пер-тенантными флагами (выключено = поведение эталона):
web_entry (VK ID-вход вне ВК + session token), degrade (экран «Продолжить в браузере»),
/go-роутер (рубильник + белый список назначений), tg_entry, tg_sender.
Замена бандла loyalupp.ru — отложена до включения tg/web-флагов.
Миграции — только аддитивные. Откат каждого механизма = выключить флаг.
