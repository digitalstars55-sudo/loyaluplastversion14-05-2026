# FILE_LAYOUT — карта репозиториев и копий LoyalUP

> Главный документ когда непонятно «где актуальная версия». Зеркало
> в [`loyalupversionMAC/FILE_LAYOUT.md`](https://github.com/digitalstars55-sudo/loyalupversionMAC/blob/main/FILE_LAYOUT.md).

## Три отдельных кодовых базы (не путать)

### 1. Backend (Django) — для всех тенантов

- **Repo (канон):** `digitalstars55-sudo/loyaluplastversion14-05-2026`
- **Прод-сервер:** `root@81.17.154.208`, `/home/levone/levelup-back/`
- **Bind-mount:** `compose.yaml` имеет `- .:/app` для web → host file = container file
- **Деплой:** правки → `git push origin main` → SSH `cd /home/levone/levelup-back && git pull`
- **Рестарт:** после `.py` → `docker kill -s HUP web`. После `.env` или `compose.yaml` → `docker compose up -d --force-recreate web`.

**Что внутри:** Django backend, 27 apps, django-tenants многотенантность, фиолетовая админка ЛоялUP, аналитика, RF-анализ, leads, mobile API, support chat relay, Expo push.

Recovery-коммит после force-recreate катастрофы: `1ebd536`.
Полная история сессии 2026-05-14: [`docs/SESSION_2026_05_14.md`](SESSION_2026_05_14.md).

### 2. Mobile client app (Expo / React Native) — для владельцев ресторанов

- **Repo:** `digitalstars55-sudo/loyalupversionMAC`
- **Внутри:** `MOBILE_TESTFLIGHT_app/rf-mobile/`
- **Деплой OTA:** `git push origin main` → `eas update --branch production --message "..."` → тестеры получают новый JS-bundle при следующем запуске
- **Нативный билд** (только при `app.json` / нативных модулях): `eas build --profile production --platform ios && eas submit --platform ios`

**Что внутри:** iOS-приложение в TestFlight для владельцев точек, личный кабинет, RF-аналитика, ChatScreen (саппорт-чат с CheckUp), Push через `expo-notifications`.

### 3. VK mini-app (Vite / React) — для гостей ресторанов

- **Repo:** отдельный (не в git'е сегодня — на сервере)
- **Сервер:** `~levone/levone-front-v3/build/`, отдаётся nginx с `levonework.ru`
- **Деплой:** rebuild + загрузка `build/` на сервер
- **Сегодня (2026-05-14) не трогали** — только CORS на бэке фиксили, чтобы мини-апс мог стучаться в API

**Что внутри:** React SPA, грузится из VK iframe (`vk.ru/app53418653`), игра, каталог призов, отзывы, ДР, реферальная система.

---

## Правило-одна-строка для каждого изменения

| Что меняю | Где работаю | Как доставить |
|---|---|---|
| Django backend | Локальная копия `levelup-back-mainNEW\` ИЛИ SSH | `git push origin main` (в этот репо) → SSH `cd /home/levone/levelup-back && git pull && docker kill -s HUP web` |
| Mobile client (Expo) | Mac `loyalupversionMAC/MOBILE_TESTFLIGHT_app/rf-mobile/` | `git push origin main` → `eas update --branch production` |
| VK mini-app гостей | На сервере `~levone/levone-front-v3/src/` | rebuild → загрузить `build/` на сервер |

---

## Если запутался — checklist

1. **Поправить чат поддержки в мобайле клиентов?**
   → `loyalupversionMAC/MOBILE_TESTFLIGHT_app/rf-mobile/src/screens/ChatScreen.tsx` → `eas update`

2. **Поправить Django endpoint для мобайла/мини-апса?**
   → Этот репо (`loyaluplastversion14-05-2026`), `apps/tenant/...` или `apps/shared/...` → `git push` → SSH pull + HUP

3. **Поправить фиолетовую админку?**
   → Этот репо, `templates/admin/base_site.html` → `git push` → SSH pull + HUP

4. **Поправить игру/каталог в VK мини-апсе гостей?**
   → `~levone/levone-front-v3/` на сервере, отдельный workflow (rebuild)

---

## Что точно НЕ трогать

- Бэкапы на проде: `/root/host-pre-rsync-*.tar.gz`, `/root/*.bak.*`, `/opt/checkup/backend/.env.prod.bak*` — страховки
- Папку `levelup-back-rf-thresholds/_ARCHIVE_DO_NOT_DEPLOY_old_backend/` в `loyalupversionMAC` — старая версия бэка
- VK мини-апс деплоить из Mac-репо — там нет его исходников; правки только в `~levone/levone-front-v3/`

---

**Связанные документы:**
- [`SESSION_2026_05_14.md`](SESSION_2026_05_14.md) — полная история восстановления после force-recreate катастрофы
- `CLAUDE.md` — конвенции проекта + «Регламент коммитов (отче наш)»
- README в `loyalupversionMAC` — детальная инструкция для Mac-репо