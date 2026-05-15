# FILE_LAYOUT — карта проекта LoyalUP (консолидировано под digitalstars55-sudo)

> Главный документ когда непонятно «где актуальная версия» / «как поднять на новой машине».
> Зеркало: backend-репо `docs/FILE_LAYOUT.md` и Mac-репо `FILE_LAYOUT.md` — синхронны.

## Весь проект — 3 кодовые базы, ВСЕ под `digitalstars55-sudo`

```
digitalstars55-sudo/
├── loyaluplastversion14-05-2026   ← 1. BACKEND (Django, прод)
├── loyalupversionMAC              ← 2. MOBILE клиента (Expo/TestFlight)
│     └── MOBILE_TESTFLIGHT_app/rf-mobile/
└── levone-front-v3                ← 3. VK МИНИ-АПС гостей (Vite/React)
```

### 1. Backend (Django) — для всех тенантов

- **Repo:** `digitalstars55-sudo/loyaluplastversion14-05-2026`
- **Прод-сервер:** `root@81.17.154.208`, `/home/levone/levelup-back/` (bind-mount `.:/app`)
- **Деплой:** `git push origin main` → SSH `cd /home/levone/levelup-back && git pull && docker kill -s HUP web`
  (после `.env`/`compose.yaml` — `docker compose up -d --force-recreate web`, не HUP)
- **Что внутри:** 27 apps, django-tenants, фиолетовая админка ЛоялUP, аналитика/RF, leads,
  mobile API, support-chat relay (CheckUp), Expo push.

### 2. Mobile client app (Expo / React Native) — для владельцев ресторанов

- **Repo:** `digitalstars55-sudo/loyalupversionMAC`, путь внутри `MOBILE_TESTFLIGHT_app/rf-mobile/`
- **Деплой OTA:** `eas update --branch production --message "..."` (для JS/TS правок — мгновенно)
- **Нативный билд** (только app.json / нативные модули / version): `eas build --profile production --platform ios && eas submit --platform ios`
- **Что внутри:** TestFlight приложение владельца: личный кабинет, RF, отзывы, рассылки,
  ChatScreen (саппорт с CheckUp), push. Гайд для Claude: `rf-mobile/CLAUDE.md`.

### 3. VK mini-app (Vite / React) — для гостей ресторанов

- **Repo:** `digitalstars55-sudo/levone-front-v3` (консолидирован 2026-05-15 из
  `stagepalete2/levone-front-v3`, история 1:1, HEAD `8f07129`)
- **Прод-сервер:** `/home/levone/levone-front-v3/`, билд `build/` отдаётся nginx с `levonework.ru`
  (это то что грузит `vk.ru/app53418653`)
- **Стек:** Vite 7, React 19, `@vkontakte/vk-bridge`, `@vkontakte/vkui`, MUI, zustand, axios
- **Деплой:** `npm run build` → залить `build/` (или `dist/`) на сервер в `/home/levone/levone-front-v3/build/`
  (nginx статику отдаёт сразу, рестарт не нужен). Либо `npm run deploy` (vk-miniapps-deploy).
- **Env:** `.env` с `VITE_BACKEND_DOMAIN` (public-домен бэка, напр. `levelupapp.ru`).
  `.env` в git НЕ хранится — на новой машине создать вручную.
- **Что внутри:** игра, каталог призов, отзывы, ДР-подарки, реферальная система.
- ⚠️ Старый `stagepalete2/levone-front-v3` оставлен как backup, не удалён.

### (бонус) Гостевой web loyalupp.ru

- `stagepalete2/levone-front-web` → сервер `~levone/levone-front-web/` → `loyalupp.ru`.
  Сегодня не консолидировали (отдельная задача при желании).

---

## КАК ПОСТАВИТЬ ВСЁ НА MAC С НУЛЯ

> Префикс токена ниже — PAT для `digitalstars55-sudo`. Все 3 репо приватные.
> Замени `<PAT>` на актуальный токен (он у пользователя).

### 0. Инструменты (один раз)

```bash
node --version      # нужен v20+
npm  --version
git  --version
npm install -g eas-cli      # для мобайла (OTA/билды)
```

### 1. Папка под всё + клонирование 3 репо

```bash
mkdir -p ~/Desktop/LOYALUP && cd ~/Desktop/LOYALUP

# 1. Backend
git clone https://digitalstars55-sudo:<PAT>@github.com/digitalstars55-sudo/loyaluplastversion14-05-2026.git backend

# 2. Mobile клиента
git clone https://digitalstars55-sudo:<PAT>@github.com/digitalstars55-sudo/loyalupversionMAC.git mobile

# 3. VK мини-апс гостей
git clone https://digitalstars55-sudo:<PAT>@github.com/digitalstars55-sudo/levone-front-v3.git vk-miniapp
```

Получится:
```
~/Desktop/LOYALUP/
├── backend/      ← Django
├── mobile/       → реальный код в mobile/MOBILE_TESTFLIGHT_app/rf-mobile/
└── vk-miniapp/   ← Vite/React гостевой
```

### 2. Backend — настройка (обычно только смотрят/правят и пушат)

```bash
cd ~/Desktop/LOYALUP/backend
# деплой = git push → на сервере git pull + HUP. Локально Django можно не запускать.
# Если нужен локальный запуск — есть docker-compose, но это тяжело и обычно не требуется.
```

### 3. Mobile клиента — настройка и запуск

```bash
cd ~/Desktop/LOYALUP/mobile/MOBILE_TESTFLIGHT_app/rf-mobile
npm install                       # (если ругань на peer-deps: npm install --legacy-peer-deps)

cat > .env << 'EOF'
EXPO_PUBLIC_API_BASE=https://levelupapp.ru
EXPO_PUBLIC_USE_MOCK=false
EOF

eas login                         # Expo-аккаунт (owner "levone")
npx expo start                    # i = iOS-симулятор, w = браузер, QR = Expo Go

# рабочий цикл:
git pull origin main
# ... правки ...
git add -A && git commit -m "..." && git push origin main
eas update --branch production --message "что сделал"   # OTA тестерам
```

### 4. VK мини-апс гостей — настройка и запуск

```bash
cd ~/Desktop/LOYALUP/vk-miniapp
npm install

# .env в git нет — создать (frontend-переменные публичны, не секрет):
cat > .env << 'EOF'
VITE_BACKEND_DOMAIN=levelupapp.ru
EOF
# (точное содержимое .env можно скопировать с сервера: /home/levone/levone-front-v3/.env)

npm run dev                       # локальный dev на http://localhost:5173
npm run build                     # прод-сборка в dist/ (или build/)

# деплой на прод:
#   scp -r dist/* root@81.17.154.208:/home/levone/levone-front-v3/build/
#   (nginx отдаёт статику сразу; перезапуск не нужен)
```

---

## Правило-одна-строка

| Меняю | Где | Доставка |
|---|---|---|
| Backend (Django) | `backend/` | `git push` → SSH `git pull && docker kill -s HUP web` |
| Mobile клиента | `mobile/MOBILE_TESTFLIGHT_app/rf-mobile/` | `git push` → `eas update --branch production` |
| VK мини-апс гостей | `vk-miniapp/` | `npm run build` → залить `build/` на сервер |

## Что НЕ трогать

- Прод-сервер вручную (только через git pull / scp build)
- Бэкапы на сервере (`/root/*.bak.*`, `/root/host-pre-rsync-*.tar.gz`)
- `loyalupversionMAC/levelup-back-rf-thresholds/_ARCHIVE_DO_NOT_DEPLOY_old_backend/`
- Старый `stagepalete2/*` — там backup-копии, не источник правды

## Ссылки

- История инцидента 2026-05-14: backend-репо `docs/SESSION_2026_05_14.md`
- Гайд для Claude в мобайле: `mobile/MOBILE_TESTFLIGHT_app/rf-mobile/CLAUDE.md`
- Конвенции бэка + «Регламент коммитов»: backend-репо `CLAUDE.md`